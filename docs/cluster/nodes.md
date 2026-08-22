# Node & Provider Selection

Objective: Pick the concrete machines for the architecture fixed in
[architecture.md](architecture.md) — which cloud provider and instance for the
internet-facing worker, how the Homelab VM is sized, and what "high
availability" means for a two-site, budget-capped cluster. Price-driven;
baseline to beat is the legacy Vultr VPS at $30/mo all-in.

> **Status**: Reviewed interactively 2026-08-21; **provider decision
> reopened 2026-08-22** — the 2026-08-21 "tentatively Hetzner CPX21 ~$14"
> call was based on pre-hike pricing; Hetzner's 2026-06-15 US price
> adjustment (~3×: CPX21 Ashburn $13.99 → $37.49) removes its advantage
> entirely. §3 is rewritten with post-hike numbers plus two newly verified
> options (GCP mixed network tiers, Oracle Always Free). All prices are
> as-of August 2026, USD, and must be re-verified in the provider
> calculators before commit. Nothing here is implemented yet.

## 1. What the cloud node actually does

Per architecture.md the cloud node is a **worker only**: the `internet-gw`
Envoy, the shared-VIP raw TCP/UDP services, KubeSpan endpoint, hath, and
whatever else is pinned to the internet pool. The control plane stays in
the Homelab. This means the cloud node needs:

-   Modest CPU/RAM: Envoy + Cilium + a handful of pods. 4 GB RAM is the
    comfortable floor (2 GB is possible but leaves no headroom for Cilium +
    Envoy + kubelet + workload bursts); 2 vCPU.
-   A **stable** public IPv4 **and** IPv6 address (dual-stack requirement);
    the primary IPs double as the `internet` LB pool VIP
    (architecture.md §3.2). Support for a reserved/floating additional IP
    is the named fallback if the pool-contains-node-IP shape misbehaves.
-   Ability to boot **Talos Linux** — this is a hard filter: the provider
    must support custom images/ISOs. This excludes AWS Lightsail (blueprint
    images only) and most "managed VPS" products.
-   Disk: 40–60 GB. Talos itself is tiny; the rest is image cache +
    local-path/Longhorn volumes for internet-pool workloads (see
    [storage.md](storage.md)).

## 2. The deciding factor is egress, not compute

Compute prices in this size class differ by ~$10/mo; egress prices differ by
~$100/mo. The legacy Vultr node quietly provided terabytes of included
transfer, and the legacy cluster leaned on that (hath via EgressGateway,
seeding). Hyperscaler pricing breaks that assumption:

| Provider path                  | Included/free egress | Overage        |
| ------------------------------ | -------------------- | -------------- |
| GCP Premium tier               | 1 GiB/mo             | $0.12/GB       |
| GCP Standard tier              | 200 GB/mo/region     | $0.085/GB      |
| AWS EC2                        | 100 GB/mo            | $0.09/GB       |
| Hetzner Cloud (US)             | 1–2 TB/mo (plan)     | ~$1–2/TB       |
| Oracle Cloud                   | 10 TB/mo             | $0.0085/GB     |
| Vultr (legacy baseline)        | 2–4 TB/mo (plan)     | $0.01/GB       |

Provider-specific facts (verified 2026-08-22):

1.  **GCP mixed network tiers are real.** Network tier is set **per access
    config**, not per VM: `--network-tier=STANDARD` for the IPv4 access
    config coexists with IPv6, whose tier "must be PREMIUM (currently only
    one value is supported)". So a dual-stack VM can put all its IPv4
    egress on Standard tier (200 GB/mo free per region, $0.085/GB after)
    while only the traffic that actually leaves over IPv6 bills at Premium
    ($0.12/GB, ~no free tier). Since hath (~50–110 GB/mo) is v4-only and
    happy-eyeballs pulls an unpredictable share of HTTP onto v6, the GCP
    bill becomes ~$24 fixed + $0.12 × (v6 share of ~100 GB HTTP) ≈
    **$24–36/mo**. Both static v4 (Standard) and static v6 (Premium) are
    reservable; external IPv6 is free, in-use IPv4 is $3.65/mo.
2.  **AWS Lightsail is disqualified** despite its bundled 1–3 TB transfer:
    it cannot boot custom images, so no Talos.
3.  **Hetzner US ≠ Hetzner EU.** The famous pricing is EU-only. US
    (Ashburn) has only the CPX/CCX lines, traffic was cut to 1–3 TB in
    Dec 2024, and the 2026-06-15 adjustment tripled US CPX prices
    (CPX11 $6.99→$20.49, CPX21 $13.99→$37.49). Two hikes in 18 months is
    also a trend signal.

### 2.1 Measured traffic (legacy cluster Prometheus, 30d to 2026-08-21)

The legacy VPS NIC moved 1276 GB TX/30d, but decomposed:

| Component | GB/30d | Fate in the new cluster |
| --- | --- | --- |
| ZeroTier tunnel encap (to homelab) | ~780 → **~150–250 steady** | becomes KubeSpan; free inside a bundled-TB plan (Vultr/Oracle), billed on GCP/AWS. Re-measured 2026-08-22 at daily granularity: quiet days are TX 2.5–10 GB / RX 5–14 GB; the 780 was migration one-offs (08-19 alone: 592 GB, the dav initial copy; 08-12: 90 GB; 08-21: 50 GB). Steady-state cross-site ≈ 150–250 GB/mo per direction — and the new design removes the shared-JuiceFS churn component (syncthing cross-site deltas remain, but they're document-sized). Re-measure over a quiet month before any hyperscaler commitment. |
| immich copytest + JuiceFS mount pods | ~500+ | one-off August migration traffic + JuiceFS churn; gone by design |
| traefik (real public HTTP serving) | **~96 TX / ~98 RX** | cloud node Envoy |
| hath (recent pod rate ~3.6 GB/day) | **~50–110 TX** | cloud node (stable IP) |

**Steady-state true public egress ≈ 150–220 GB/mo.** Design rules
(reflected in architecture.md §3.4):

-   **Bulk-egress workloads (qbittorrent/seeding, large syncs) are pinned to
    the Homelab pool and egress directly via the home uplink**, never
    through a metered cloud path.
-   **hath is the only workload requiring a stable public IP** (inbound port
    and outbound on the same IP). It runs on the cloud node; its ~50–110
    GB/mo fits trivially inside any bundled-TB allowance, and its 50 Gi
    cache fits an 80 GB instance disk.

## 3. Provider comparison (cloud worker, dual-stack, Talos)

Monthly cost = instance + boot disk + IPv4 + egress at three traffic points.
Instance prices are 1-year committed/reserved where available (3-year rates
are ~30% lower still; defer committing until the design has run for a few
months).

| | GCP e2-medium (mixed tier) | AWS t4g.medium | Hetzner CPX21 (US) | Vultr 4 GB | Oracle A1.Flex (free) |
| --- | --- | --- | --- | --- | --- |
| Specs | 2 shared vCPU / 4 GB | 2 vCPU ARM / 4 GB | 3 vCPU / 4 GB | 2 vCPU / 4 GB | ARM, up to 4 OCPU / 24 GB |
| Instance (1y commit) | $15.41 | ~$16 | $37.49 (no commit) | ~$24 (no commit) | $0 |
| Boot disk 50 GB | $5.00 (pd-balanced) | $4.00 (gp3) | included (80 GB) | included | included (200 GB total free) |
| Public IPv4 | $3.65 | $3.65 | $0.60 | included | $0 |
| Fixed subtotal | **$24.06** | **$23.65** | **~$38.09** | **~$24** | **$0** |
| @ 220 GB egress (steady state, §2.1) | $24.06 + $0.12 × v6-share ≈ **$24–36** | ~$34.45 | $38.09 | $24 | $0 (10 TB incl.) |
| KubeSpan inter-node TX (~150–250 GB/mo steady, 600 GB/day spikes) | metered on v4 Standard — competes with public traffic for the 200 GB allowance | metered $0.09/GB ⇒ +$15–25 steady, spikes billed | included | included (2–3 TB pool) | included |
| Talos support | image upload | AMI upload | no ISO — rescue-mode dd / packer snapshot | **native custom ISO** | custom image (.oci qcow2), official guide |
| Dual-stack, stable IPs | static v4 (Standard) + v6 (Premium) reservable | EIP + stable v6 | primary IPs detachable, v6 /64 free | reserved IPs (v4 + v6) | reserved v4; v6 on dual-stack VCN |
| Additional IP (VIP fallback) | static IP + alias/forwarding rules | EIP re-assignable | floating v4 $3.50 / v6 $1.50 | reserved IP | reserved public IP |
| Risk notes | v6-share billing unpredictable; CUD lock-in | KubeSpan metering kills it | 2 hikes in 18 mo; US traffic cut to 1–2 TB | legacy incumbent; pricing stable through 2024–2026 | **platform risk**: capacity hunts, 2026-06 free-tier halving (2→12 GB now; PAYG keeps 4/24), reports of account purges; PAYG conversion mitigates |

The KubeSpan row matters more for its *variance* than its magnitude:
steady-state cross-site TX is ~150–250 GB/mo (re-measured 2026-08-22,
§2.1), which on AWS adds ~$15–25/mo and on GCP eats most of the
Standard-tier free allowance — but single migration/repair days have hit
600 GB, and on a metered provider every future data move, restore drill,
or resync becomes a billing decision. A bundled multi-TB pool (Vultr,
Oracle) makes cross-site traffic architecturally free, so the design
never has to think about it.

### 3.1 Decision (2026-08-22: reopened, recommendation pending user review)

Post-hike landscape at measured traffic (220 GB public + ~150–250 GB
KubeSpan TX steady state, with multi-hundred-GB spike days):

-   **Hetzner CPX21 US $38/mo**: no longer competitive; also no native
    ISO and a worrying pricing trend. Dropped.
-   **AWS**: ~$34 + ~$15–25 KubeSpan metering + billed spikes ≈ $50+.
    Dropped.
-   **GCP mixed-tier**: base $24, but public v4 + KubeSpan together
    overflow the 200 GB Standard allowance and v6 share bills at Premium
    — realistically $35–45 and unpredictable. Dropped as primary.
-   **Vultr 4 GB (~$24/mo)**: bundled 2–3 TB covers public + KubeSpan
    traffic with headroom; native ISO Talos install; mature
    pulumi/terraform provider; and it is the *legacy incumbent* — the
    original "$30 baseline to beat" is beaten by simply right-sizing the
    plan on the same provider, with zero provider-migration risk.
-   **Oracle A1.Flex (free, 4 OCPU/24 GB, 10 TB)**: spec-dominant and $0,
    but Oracle unilaterally halved the free tier in June 2026 and
    reclaims/purges idle free tenancies; acceptable only with PAYG
    conversion, and never as the sole public ingress.

**Recommendation: Vultr 4 GB as the cloud worker** (boring, cheap enough,
known-good). Optional second step once the cluster is stable: add an
Oracle A1 node as a *free experiment* — extra egress capacity, a
cold-standby CP substrate (nodes.md §5 Tier 0), or public-ingress Tier 2 —
designed so its disappearance is a non-event. Final call is the user's,
at the end-of-design total-cost pass.

## 4. Homelab node(s)

The Homelab side runs on the existing physical server under libvirt
(pulumi-libvirt, macvlan/bridge to LAN per architecture.md §5). Sizing is
derived from a host inventory, not guessed.

### 4.1 Host inventory (measured 2026-08-22, Aetf-Arch-Homelab)

**Hardware**: i9-12900KS (16C/24T), **32 GB RAM** (the binding
constraint), 465 GB NVMe (system + legacy k3s data; ~85 GB free while the
legacy cluster still runs), ZFS pool `nas` (80 TB raw, 57 TB used) on the
JBOD — this host *is* the NAS.

**Stays on the host** (never enters the cluster):

| What | Why it stays | RAM budget |
| --- | --- | --- |
| ZFS + NFS + Samba serving | the NAS role; the cluster consumes it | ZFS ARC (in-RAM read cache) ≥4 GiB (currently squeezed to ~2) |
| HAOS libvirt VM (2 vCPU / 4 GiB, PCIe USB3 + WiFi/BT passthrough) | home automation must survive cluster outages; architecture.md §6.8 | 4 GiB |
| adguardhome-sync (podman); AdGuard alice/bob themselves run as nspawn containers **on the UDM**, not here | LAN DNS lives on the gateway and must survive cluster (and this host's) outages | ~0.1 GiB |
| Pulumi state-backend Postgres (podman) | cannot live inside the cluster it manages | ~0.2 GiB |
| zerotier, sshd, apcupsd, smartd, syslog-ng, small relays (jellyfin-discovery, samsung-tv), Claude sessions | host plumbing / management path | ~1.5 GiB |

**Moves into the cluster**: everything currently in legacy k3s on this
host (requests 12.5 GiB, actual ~16 GiB including k3s overhead:
immich+CNPG, jellyfin+Shoko, prometheus+grafana, syncthing-nas, hath is
cloud-bound, …) **plus the natives that only stayed out for platform
reasons**: qbittorrent-nox (blocked on IPv6, solved by architecture.md
§3.5) with seedwatch alongside it (it drives the qbittorrent API and
reads NAS hardlink counts — both reachable in-cluster), and
thread-dashboard. distccd is retired or containerized opportunistically.

**GPU**: legacy workloads hold 3× `gpu.intel.com/i915` allocations
(immich ML/transcode, jellyfin). Plan of record: VFIO-passthrough the
UHD 770 into the Talos VM (host is headless) + Intel device plugin in
the guest; fallback is CPU transcode at reduced quality-of-life. This is
a migration-blocking item to verify early, not late.

### 4.2 VM sizing

-   **CP/worker VM**: **12–16 vCPU / 20 GiB RAM**, disk on local NVMe.
    RAM math: 32 − 4 (HAOS) − 4 (ARC) − 2 (host+podman) − ~1 (qemu/host
    overhead) ≈ 21 GiB. That covers today's ~16 GiB workload set plus
    Longhorn's 2–4 GiB (storage.md §3) with little slack — **a RAM
    upgrade is the designated relief valve**, cheaper than any
    architectural workaround (board verified 2026-08-22: ROG Maximus
    Z690 Hero, DDR5, 4×DIMM up to 128 GB).
-   **Disk**: target 100+ GB qcow2/raw on NVMe, but only ~85 GB is free
    while both clusters coexist. The migration plan must interleave
    reclamation (legacy images, local-path PVCs, prometheus's 30 GB) with
    VM growth — start at ~60 GB, grow after cutover. etcd wants <10 ms
    fsync — keep its disk on NVMe, never on ZFS-over-JBOD, network, or
    USB storage.
-   **Optional second worker VM** (2–4 vCPU / 8 GB): unchanged from the
    2026-08-21 decision — **not in the initial build**; it is the only
    path to same-site Longhorn `replica=2` and drain-without-evict, and
    RAM is why it waits for the RAM upgrade. The design must not assume
    it exists.

### 4.3 One big VM, not several small ones

Decided 2026-08-22 (was an open question): a single large VM. On one
physical host, multiple VMs add no real fault isolation but each costs a
fixed overhead (kubelet + Cilium + OS ≈ 1 GiB, plus a Longhorn
instance-manager CPU reservation per node) from a 32 GB budget that has
none to spare. The legitimate second-VM use cases (Longhorn replica=2,
maintenance drains) are exactly the deferred §4.2 option — one *more* VM
later, not N small ones now.

## 5. High availability, honestly

The requirement is "high availability"; architecture.md §6.2 already rejected
a WAN-stretched etcd, and that rejection stands — nothing at this budget
makes cross-site Raft good. What HA means here, in tiers:

-   **Tier 0 — declarative rebuild + backups (adopted)**: the cluster *is*
    the Pulumi program plus Talos machine configs. HA against permanent loss
    comes from: hourly etcd snapshots shipped to object storage (Talos
    `etcd snapshot` or the built-in snapshotter), Longhorn backups and CNPG
    barman to the same bucket (storage.md §5), and a periodically *drilled*
    restore (same discipline as the legacy CNPG restore drill). Target:
    RPO ≤ 1 h, RTO ~1–2 h hands-on. This explicitly includes a
    **control-plane cold-standby drill**: re-bootstrapping a temporary CP
    from the latest etcd snapshot onto a substitute node (e.g., the cloud
    worker) in ~30–60 min, covering the extended-home-outage case without
    paying for a cloud control plane (architecture.md §6.5).
-   **Tier 1 — workload HA (adopted where it matters)**: apps that support
    replication run multi-replica across the two pools (CNPG multi-instance,
    stateless apps ×2). A control-plane outage does not stop running
    workloads — kubelet static pods and Cilium keep forwarding; what stops
    is scheduling and API access.
-   **Tier 2 — public-ingress HA (deferred option)**: DNS points at one
    cloud node; that node is a SPOF for public HTTP. If that ever matters,
    add a second small cloud instance in another zone + health-checked DNS.
    Costs one more instance + IPv4. Not in the initial build.
-   **Tier 3 — control-plane HA (rejected again)**: 3 CP VMs on the single
    Homelab host protect only against guest-OS crashes while tripling etcd's
    RAM/IO footprint on one physical SPOF; 3 CP nodes across sites is §6.2.
    Neither pays for itself.

## 6. Alternatives Considered

-   **AWS Lightsail (dual-stack $5–12/mo, 1–3 TB bundled)**: best sticker
    price of any hyperscaler product, rejected because custom images cannot
    be booted — no Talos. Revisit only if the Talos requirement ever falls.
-   **GCP t2a (ARM Tau)**: no CUD pricing at all ($56/mo on-demand for
    2 vCPU/8 GB) and limited regions; strictly dominated by e2 + CUD.
-   **GCP free-tier e2-micro**: free but 1 GB RAM — cannot host Cilium +
    Envoy + workloads. Usable later as a free external probe/uptime checker,
    not as a cluster node.
-   **Spot/preemptible for the ingress node**: e2-medium spot ($14.68) barely
    beats the 1y CUD ($15.41) and brings preemption-induced public downtime.
    Rejected.
-   **IPv6-only cloud node** (drop the $3.65 IPv4): rejected for the same
    reasons as architecture.md §6.1 — public ingress must serve IPv4 users,
    and registries/apps still assume IPv4.
-   **Hetzner EU regions** (Falkenstein/Helsinki, CX32-class ~€7 + 20 TB):
    the pricing that made Hetzner attractive still exists — in Europe.
    Rejected for +90–100 ms RTT to the US home site on every KubeSpan
    packet and for US visitors; the WAN-latency budget is already the
    architecture's scarcest resource.
-   **netcup Manassas (~$6–8, generous traffic)**: attractive sticker,
    rejected on IaC immaturity — no credible terraform/pulumi provider,
    and this cluster's premise is that everything is declared.
