# Node & Provider Selection

Objective: Pick the concrete machines for the architecture fixed in
[architecture.md](architecture.md) — which cloud provider and instance for the
internet-facing worker, how the Homelab VM is sized, and what "high
availability" means for a two-site, budget-capped cluster. Price-driven;
baseline to beat is the legacy Vultr VPS at $30/mo all-in.

> **Status**: as decided 2026-08-22 — **cloud site = OCI, 3× A1.Flex
> (1 OCPU / 8 GB) combined control-plane + ingress nodes** (§3.1–3.2);
> homelab VM is a pure worker (§4); Vultr is the scripted fallback. All
> prices are as-of August 2026, USD, verified against official price
> pages/APIs, and must be re-verified in the provider calculators before
> commit. Nothing here is implemented yet. (Decision history: AWS →
> GCP (2026-04) → tentatively Hetzner (2026-08-21, voided by its
> 2026-06-15 US price hike) → OCI.)

## 1. What the cloud pool actually does

Per architecture.md §1.1 the cloud site is **three combined
CP+ingress+worker nodes**: the etcd/apiserver quorum, the `internet-gw`
Envoy replicas, the shared-VIP raw TCP/UDP services behind the NLB,
KubeSpan endpoints, hath (pinned to one node), and the internet-pool
workloads. This means the cloud pool needs:

-   **Per node**: ~2–2.5 GB for etcd+apiserver+controllers, ~1.5 GB
    platform floor (Talos/kubelet/Cilium/Envoy), leaving ~4 GB of an
    8 GB node for workloads. One dedicated Ampere core per node covers
    steady-state CP load at this cluster size (the whole legacy VPS node
    idles at ~0.4 cores); workload CPU limits keep bursts away from etcd
    (architecture.md §6.5 for why this budget, honestly kept, avoids the
    legacy CP-starvation failure).
-   **Stable public dual-stack IPs** per node (their on-the-wire forms —
    the v4 *private* primaries behind OCI's 1:1 NAT, plus the v6 GUAs —
    double as the `internet` pool VIPs, architecture.md §3.2) plus the
    NLB as the DNS-stable front.
-   Ability to boot **Talos Linux** — a hard filter: the provider must
    support custom images/ISOs (excludes AWS Lightsail and most managed
    VPS products).
-   **Disk**: ~50 GB boot per node; hath's cache as a block volume on
    its gateway node. 3 × boot + hath ≈ the 200 GB free block allowance —
    slight paid overflow is cents (§3.2). etcd lives on the boot volume:
    **verify fsync latency <10 ms at bootstrap** (OCI Balanced block
    volumes are typically ~1–2 ms; this is a check, not an assumption).

## 2. The deciding factor is egress, not compute

Compute prices in this size class differ by ~$10/mo; egress prices differ by
~$100/mo. The legacy Vultr node quietly provided terabytes of included
transfer, and the legacy cluster leaned on that (hath via EgressGateway,
seeding). Hyperscaler pricing breaks that assumption:

| Provider path                  | Included/free egress | Overage        |
| ------------------------------ | -------------------- | -------------- |
| GCP Premium tier               | 1 GiB/mo             | $0.12/GB       |
| GCP Standard tier              | 200 GiB/mo (per billing account, ALL regions combined) | $0.085/GiB |
| AWS EC2                        | 100 GB/mo (all services/regions combined) | $0.09/GB |
| Hetzner Cloud (US)             | 1–3 TB/mo (CPX11/21/31) | $1/TB      |
| Oracle Cloud                   | 10 TB/mo (per tenancy) | $0.0085/GB   |
| Vultr (legacy baseline)        | 2–5 TB/plan, pooled account-wide, +2 TB free/account | $0.01/GB |

Provider-specific facts (verified 2026-08-22 against official price
pages/APIs; the deep-verification pass re-confirmed every number below
from primary sources — Hetzner's price-adjustment doc, Vultr's live
`api.vultr.com/v2/plans`, GCP's served pricing HTML, AWS's awsstatic
pricing feeds, Oracle's cetools price-list API):

1.  **GCP mixed network tiers are real.** Network tier is set **per access
    config**, not per VM: `--network-tier=STANDARD` for the IPv4 access
    config coexists with IPv6, whose tier "must be PREMIUM (currently only
    one value is supported)". So a dual-stack VM can put all its IPv4
    egress on Standard tier (**200 GiB/mo free per billing account across
    all regions — not per region**; $0.085/GiB after) while only the
    traffic that actually leaves over IPv6 bills at Premium ($0.12/GiB,
    ~no free tier). Since hath (~50–110 GB/mo) is v4-only and
    happy-eyeballs pulls an unpredictable share of HTTP onto v6, the GCP
    bill becomes ~$24 fixed + $0.12 × (v6 share of ~100 GB HTTP) ≈
    **$24–36/mo**. Both static v4 (Standard) and static v6 (Premium) are
    reservable; external IPv6 is free, in-use IPv4 is $3.65/mo. (Prices
    are us-central1; us-east4 runs ~10% higher, unverified.)
2.  **AWS Lightsail is disqualified** despite its bundled 1–3 TB transfer:
    it cannot boot custom images, so no Talos.
3.  **Hetzner US ≠ Hetzner EU.** The famous pricing is EU-only. US
    (Ashburn) has only the CPX/CCX lines, traffic was cut to 1–3 TB in
    Dec 2024, and 2026 brought **two** hikes (2026-04-01, then
    2026-06-15: CPX11 $6.99→$20.49, CPX21 $13.99→$37.49, CPX31
    $24.99→$73.49 — confirmed on Hetzner's own price-adjustment doc,
    ~4× vs early 2026 combined). Existing servers keep old pricing but
    **any rescale re-prices to the new list**.
4.  **Vultr's 4 GB tier is three product lines**: Regular `vc2-2c-4gb`
    $20 (80 GB SSD, 3 TB), High Frequency $24 (128 GB NVMe, 3 TB),
    High Performance `vhp` $24 (100 GB NVMe, **5 TB**). Transfer is
    pooled account-wide with an extra 2 TB/mo free per account; overage
    $0.01/GB. Reserved IPs are **$3/mo flat, attached or not** (the old
    "free while attached" rule is gone from current docs). Custom ISO
    upload free (≤10 GB, 2/account); snapshots $0.05/GB-mo. Compute
    pricing unchanged since the Jan 2023 restructure. One real demerit:
    the **Pulumi provider is a community bridge** (ediri/pulumiverse,
    lagging) — only the Terraform provider is Vultr-official.

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
    and outbound on the same IP; the dedicated-VIP pattern,
    architecture.md §3.2). Its ~50–110 GB/mo fits trivially inside the
    10 TB allowance, and its 50 Gi cache rides a block volume inside the
    free 200 GB.

## 3. Provider comparison (cloud worker, dual-stack, Talos)

Monthly cost = instance + boot disk + IPv4 + egress at three traffic points.
Instance prices are 1-year committed/reserved where available (3-year rates
are ~30% lower still; defer committing until the design has run for a few
months).

| | GCP e2-medium (mixed tier) | AWS t4g.medium | Hetzner CPX21 (US) | Vultr 4 GB | Oracle A1.Flex (free) |
| --- | --- | --- | --- | --- | --- |
| Specs | 2 shared vCPU / 4 GB | 2 vCPU ARM / 4 GB | 3 vCPU / 4 GB | 2 vCPU / 4 GB | ARM, 2 OCPU / 12 GB (4 / 24 under PAYG, eroding) |
| Instance (1y commit) | $15.41 (3y: $11.01) | $15.40 (1y RI no-upfront) | $37.49 (no commit) | $20 vc2 / $24 vhp-5TB (no commit) | $0 |
| Boot disk 50 GB | $5.00 (pd-balanced $0.10/GiB) | $4.00 (gp3 $0.08/GB) | included (80 GB) | included (80–100 GB) | free (within 200 GB boot+block allowance, home region) |
| Public IPv4 | $3.65 ($0.005/hr) | $3.65 ($0.005/hr, idle same) | $0.60 | included | $0 (reserved & idle also $0; 50/region limit) |
| Fixed subtotal | **$24.06** | **$23.05** | **~$38.09** | **$20–24** | **$0** |
| @ 220 GB egress (steady state, §2.1) | $24.06 + $0.12 × v6-share ≈ **$24–36** | ~$33.85 | $38.09 | $20–24 | $0 (10 TB incl.) |
| KubeSpan inter-node TX (~150–250 GB/mo steady, 600 GB/day spikes) | metered on v4 Standard — competes with public traffic for the 200 GB allowance | metered $0.09/GB ⇒ +$15–25 steady, spikes billed | included | included (2–3 TB pool) | included |
| Talos support | image upload | AMI upload | no ISO — rescue-mode dd / packer snapshot | **native custom ISO** | custom image (.oci qcow2), official guide |
| Dual-stack, stable IPs | static v4 (Standard) + v6 (Premium) reservable | EIP + stable v6 | primary IPs detachable, v6 /64 free | reserved IPs (v4 + v6) | reserved v4; v6 on dual-stack VCN |
| Additional IP (VIP fallback) | static IP + alias/forwarding rules | EIP re-assignable | floating v4 $3.50 / v6 $1.50 | reserved IP | reserved public IP |
| Risk notes | v6-share billing unpredictable; CUD lock-in | KubeSpan metering kills it | 2 hikes in 18 mo; US traffic cut to 1–2 TB | legacy incumbent; pricing stable through 2024–2026 | **platform risk**: capacity hunts, 2026 free-tier halving (Always-Free → 2 OCPU/12 GB, enforced 2026-08-18; PAYG holds 4/24 for now), reports of account purges; PAYG conversion mitigates |

The KubeSpan row matters more for its *variance* than its magnitude:
steady-state cross-site TX is ~150–250 GB/mo (re-measured 2026-08-22,
§2.1), which on AWS adds ~$15–25/mo and on GCP eats most of the
Standard-tier free allowance — but single migration/repair days have hit
600 GB, and on a metered provider every future data move, restore drill,
or resync becomes a billing decision. A bundled multi-TB pool (Vultr,
Oracle) makes cross-site traffic architecturally free, so the design
never has to think about it.

### 3.1 Decision (2026-08-22): OCI selected, Vultr as scripted fallback

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
-   **Oracle A1.Flex under PAYG (free, 10 TB egress)**: the risk profile
    changes materially once the tenancy is converted to Pay-As-You-Go
    with a card on file (one-time $100 verification hold, refunded),
    *while still paying $0* for Always-Free-shaped usage:
    -   **Design against 2 OCPU / 12 GB, treat 4 / 24 as an eroding
        bonus.** The 2026 halving cut Always-Free tenancies to
        2 OCPU / 12 GB (enforced by termination from 2026-08-18); PAYG
        tenancies still hold 4 OCPU / 24 GB *as of August 2026*, but
        contemporary coverage expects PAYG to converge to the same
        limits eventually — the architecture must stay valid at 12 GB
        (it does: platform floor ~2 GiB + cloud workloads + sidecar fit
        with room to spare, §4.4). Mechanically the allowance is
        1,500 OCPU-hours + 9,000 GB-hours/month across A1 VMs; PAYG is
        billed (not killed) beyond it.
    -   PAYG tenancies are **exempt from the idle-reclaim policy** (the
        documented 7-day/95th-percentile-CPU rule targets Always Free
        accounts).
    -   The reported no-warning account purges cluster heavily on
        free-only tenancies; a paying account has normal customer
        standing.
    -   The failure mode inverts: on a limits change, a PAYG tenancy gets
        **billed instead of killed** — a survivable surprise, bounded by
        billing alerts, versus a terminated node.
    -   PAYG also opens *paid headroom on the same node*: extra A1 OCPUs/
        RAM and block storage beyond the free 200 GB are metered cheaply
        if ever needed.
    -   Residual risks that don't go away: A1 **capacity hunts apply at
        (re)creation time** (a running instance is safe; replacing it in
        a full region may take days), Oracle's demonstrated appetite for
        unilateral term changes, and ARM-only (fine for Talos and
        everything in the current workload set; hath is Java, syncthing
        Go — both multi-arch).

**Decided: OCI A1.Flex under PAYG, as the 3-node combined pool of §1.**
The deciding observations: (a) the §4.4 analysis shows cloud-node cost
is dictated by the fixed platform floor, which free 8 GB shapes simply
delete as a concern; (b) A1 billing's fungibility turns the free/cheap
allowance into *three* nodes — buying CP HA and ingress HA that no paid
single-instance alternative offers at any similar price (§3.2); (c)
this cluster's Tier-0 posture (§5: everything declarative, drilled
rebuild, backups off-provider) is precisely the design that makes OCI's
platform risk survivable — the Vultr fallback (vhp 4 GB, $24, single
node, ingress-only + homelab CP per architecture.md §6.5's superseded
layout) is a stack config flip plus a DNS diff, exercised as a drill
like every other restore path.

### 3.2 OCI deep dive: commercial model, service landscape, gotchas

Verified 2026-08-22 against Oracle's price-list API (cetools, the cost
estimator's backend, data stamped 2026-08-14), the billing guide, the
July 2026 Pillar SLA document, and the Always Free docs. Uniform list
pricing across all commercial regions (no US exception found).

**Commercial model — PAYG is the only sensible one at this scale.** The
alternatives are commit models: Annual Universal Credits (12-month
prepaid pool, unused credits forfeited, discount schedule not public)
and Monthly Flex (subject to Oracle approval). The old startup-credit
program no longer exists on the official page. PAYG specifics:

-   Billed monthly in arrears; $100 one-time card authorization at
    upgrade; the $300/30-day trial credit survives conversion.
-   **Support is included** in service fees on PAYG (no 3–10%-of-spend
    surcharge like other clouds); Always-Free-only tenancies get no
    ticket support.
-   **Paid usage carries financially-backed SLAs** (compute single
    instance: 99.9% monthly uptime → 100% credit below it; Object
    Storage 99.9%); Always Free resources explicitly carry **no SLA**.
    One more concrete thing the $0-but-PAYG posture buys.
-   **There is no hard spending cap.** Budgets are alerts only,
    evaluated every ~24 h. The real guardrails are resource-side:
    **compartment quotas + service limits** pinning creatable shapes to
    the free envelope, plus budget alerts. Accept this consciously —
    "billed, not killed" means the bill is the failure mode.

**Free-envelope facts that matter to this architecture** (beyond the A1
shape itself):

-   Public IPv4: $0, including reserved *and idle* (50/region limit).
    IPv6: **/56 GUA per VCN**, $0. VCN/IGW/NAT/service gateway/S2S VPN:
    all $0. In-region transfer (node ↔ Object Storage): $0.
-   Block storage: **200 GB total (boot + block, home region)** free +
    5 volume backups — covers the Talos boot disk *and* hath's 50 Gi
    cache at $0. Beyond: $0.0255/GB-mo (+$0.017 at Balanced VPU).
-   **Bastion service: free** — an out-of-band SSH path to the node
    when the cluster/KubeSpan is down. Vault software keys + secrets:
    free. NLB ×1 + 10 Mbps flexible LB ×1: free (unused by the Cilium
    design, but there).
-   **OCIR** (container registry): billed as Object Storage (first
    10 GB free) — a natural same-region home for homelab-containers
    images consumed by the cloud node.
-   Monitoring/Notifications free tiers are huge (500M datapoints/mo,
    1M HTTPS deliveries) — good for an **out-of-band alert channel**
    (MQL, not PromQL; no substitute for VictoriaMetrics).
-   Email Delivery: 3,000 mails/mo free via port 587 (approved-sender
    setup required); **outbound port 25 is tenancy-blocked by default**.

**Paid rates around the free envelope** (for headroom math): A1
$0.01/OCPU-h + $0.0015/GB-h; preemptible capacity is a flat −50%;
E4 $0.025/OCPU-h. Egress beyond 10 TB: $0.0085/GB (NA). **Compute
capacity reservations exist** (unused capacity bills at 85%) — a priced
hedge against the A1 re-creation capacity hunt at worst-case ~$47/mo
for 4/24; not recommended (the Vultr fallback is cheaper insurance),
but it exists if OCI ever becomes load-bearing.

**The node-shape math (accepted posture: "we always pay something",
budget ~$20/mo).** A1 billing is *fungible*: OCPU-hours and GB-hours
accrue per tenancy across all A1 instances, free tier subtracted from
the totals — shapes are freely splittable across nodes. At 744 h/mo the
marginal prices are **$7.44 per always-on OCPU** and **$1.116 per
always-on GB**. The as-designed shape:

| Shape | Total | Cost under 3,000/18,000 free (current billing) | Under 1,500/9,000 (conservative) |
| --- | --- | --- | --- |
| **3 × (1 OCPU / 8 GB)** — as designed | 3 OCPU / 24 GB | **$0** | **$20.83/mo** |
| bump one node +1 OCPU (if hath's node runs hot) | 4 / 24 | $0 | $28.27/mo |

(The estimator still prices 4 OCPU/24 GB at $0 because the price API's
free tiers are still 3,000/18,000 — data stamped 2026-08-14 — while the
docs already say 1,500/9,000: a docs-first rollout of the halving. The
design is budgeted against the conservative column.)

What the three-way split buys over one big node — each a standing
benefit, so the ~2× platform-floor overhead (3 × ~1.5 GiB instead of 1)
and two more Talos nodes to patch pass the §4.4 standing-rent test:

-   **Control-plane HA** (etcd quorum, architecture.md §1.1) and
    **public-ingress HA** (all nodes behind the free NLB) in one move.
-   **Maintenance without downtime of anything** — drain/upgrade one
    node while two serve; quorum holds at 2/3.
-   **A continuous capacity hold**: the practical mitigation for the A1
    re-creation hunt is never needing to create more than one node at
    once; losing one leaves quorum and ingress up while the replacement
    hunts for capacity.

**Gotchas registry**: new-tenancy service limits are low and free-tier
limit increases are often refused (PAYG requests go through); Always
Free resources are mostly home-region-locked and the home region is
immutable at signup; the A1 free allowance is measured in OCPU-hours
(1,500 or 3,000/mo — see the eroding-allowance note above), so a
deleted-and-recreated instance burns the same pool; watch the
31/90-day minimum-retention rules if Object Storage IA/Archive tiers
ever get used.

## 4. Homelab node(s)

The Homelab side runs on the existing physical server under libvirt
(pulumi-libvirt; the VM's concrete disk/network/GPU shape is
physical/homelab-host.md). Sizing is
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
| AdGuard alice/bob run as nspawn containers **on the UDM**, not here; the host's adguardhome-sync **retires** once Pulumi dual-writes both instances (declarative/dns.md §3) | LAN DNS lives on the gateway and must survive cluster (and this host's) outages | ~0 |
| The *legacy* Pulumi state-backend Postgres (podman) — serves kluster-code until decommission; the new cluster's backend lives on an OCI micro (framework/ci.md §1) | a state backend cannot live inside the cluster it manages | ~0.2 GiB (until Wave F) |
| zerotier, sshd, apcupsd, smartd, syslog-ng, small relays (jellyfin-discovery, samsung-tv), Claude sessions | host plumbing / management path | ~1.5 GiB |
| dmarc-check timer (claude-driven DMARC triage) | uses the host's claude credentials (an in-cluster CronJob would need the API key sealed); revisit after cluster stabilization — a different approach may supersede it | ~0 |

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
the guest; fallback is CPU transcode at reduced quality-of-life. The
capability is verified early on a scratch VM; the actual bind is a
scheduled migration cutover (physical/homelab-host.md §3, migration.md Wave C).

### 4.2 VM sizing

-   **Worker VM** (pure worker — the control plane lives cloud-side,
    architecture.md §1.1): **12–16 vCPU / 20 GiB RAM**, disk on local
    NVMe. RAM math: 32 − 4 (HAOS) − 4 (ARC) − 2 (host+podman) − ~1
    (qemu/host overhead) ≈ 21 GiB. **20 GiB is the end state, not the
    boot size**: while legacy k3s still holds ~16 GiB the VM starts at
    ~10 GiB and grows stepwise as each migration wave stops its legacy
    workloads (migration.md §0.4 — RAM is interleaved exactly like the
    NVMe below). Today's ~16 GiB in-cluster usage
    already includes ~5 GiB of infra tax that the economy program (§4.4)
    shrinks — and with etcd/apiserver moved off this VM its share drops
    further (kubelet+containerd+Cilium ≈ 1.5 GiB) — so ~20 GiB carries
    the workload set plus qbittorrent with comfortable slack. **A RAM
    upgrade remains the relief valve** (board verified: ROG Maximus
    Z690 Hero, DDR5, 4×DIMM up to 128 GB).
-   **Disk**: target 100+ GB on NVMe (concrete shape — raw sparse file
    on a nodatacow subvolume, virtio-blk — physical/homelab-host.md §1), but only ~85 GB is free
    while both clusters coexist. The migration plan must interleave
    reclamation (legacy images, local-path PVCs, prometheus's 30 GB)
    with VM growth — start at ~60 GB, grow after cutover. (No etcd on
    this VM; its disk constraint is ordinary workload I/O.)
-   **Optional second worker VM** (2–4 vCPU / 8 GB): **not in the
    initial build**; it is the only path to same-site storage replicas
    and homelab drain-without-evict, and RAM is why it waits for the RAM
    upgrade. The design must not assume it exists.

### 4.3 One big VM, not several small ones

Decided 2026-08-22 (was an open question): a single large VM. On one
physical host, multiple VMs add no real fault isolation but each costs a
fixed overhead (kubelet + Cilium + OS ≈ 1 GiB per node) from a 32 GB
budget that has none to spare. The legitimate second-VM use cases
(same-site storage replicas, maintenance drains) are exactly the deferred
§4.2 option — one *more* VM later, not N small ones now.

### 4.4 Infrastructure tax, measured, and the economy program

The "how much of the machine is control plane / infra?" question,
answered with `kubectl top` on the legacy cluster (2026-08-22), of
~16 GiB in use on the homelab node:

| Infra component | Measured | Fate in the new cluster |
| --- | --- | --- |
| k3s server + containerd (host procs) | ~1.7 GiB | CP moved cloud-side; the homelab VM keeps only kubelet+containerd (~0.5 GiB). The CP cost re-appears as ~2–2.5 GiB × 3 on the cloud nodes — paid out of free OCI RAM, not out of the 32 GB host |
| Monitoring (prometheus 948 Mi, grafana 428 Mi, exporters/operator/alertmanager ~200 Mi) | ~1.6 GiB | **switch to VictoriaMetrics** (vmsingle + vmagent + vmalert): PromQL-compatible, typically ~1/5 the RAM at this scale; grafana stays. Target ≤0.7 GiB |
| Shared-JuiceFS stack (CSI ×5, redis, 4 mount pods) | ~1.0 GiB | CSI/redis/dashboard gone by design; one per-app sidecar mount remains, **budgeted honestly at 0.5–1 GiB requests** — root cause (b) of the legacy instability was starving exactly this process (storage.md §6) |
| Everything else in kube-system (coredns, metrics-server, cert-manager, sealed-secrets, nfd, local-path, reloader) | ~0.45 GiB | kept as-is — **sealed-secrets stays** (the secret-management model is unchanged from kluster-code); only the JuiceFS dashboard is dropped |
| CNPG operator + traefik | ~0.3 GiB | CNPG stays; traefik → the two Envoy gateways (similar) |
| **Total** | **~5 GiB (~30%)** | **projected ~4–4.5 GiB** |

The projection *includes* what the new design adds (Cilium agents are
heavier than flannel+kube-proxy, two gateways, VolSync controller) and
excludes what it refuses: Longhorn's 2–4 GiB was the largest single new
infra item on the table and is deferred with criteria (storage.md §3.2).
Rule going forward: **standing infra must pay standing rent** — a
component that idles waiting for rare events (mobility layers,
dashboards nobody opens) is bought as a procedure (restore, redeploy)
instead of a daemon.

Cloud-node corollary — and the real point of this section: **cloud-node
size is set by the fixed floors, not by workloads**. Platform floor
(Talos + kubelet + Cilium + Envoy) is ~1.5 GiB; the CP slice
(etcd+apiserver+controllers) another ~2–2.5 GiB — before the first app
pod. This is what made 2–4 GB paid instances structurally cramped and
what the 8 GB A1 nodes absorb for free: each still keeps ~4 GiB for
hath (~0.3), syncthing/dav (~0.3), the JuiceFS sidecar at its honest
0.5–1 GiB request, and Envoy. Meanwhile the homelab side, though
tighter on paper, is the flexible one: workloads can be trimmed, ARC
squeezed, and RAM added (§4.2) — memory pressure there is real but
manageable.

## 5. High availability, honestly

What HA means here, in tiers — the 3-node cloud pool delivers Tiers 2–3
by construction; Tier 0 remains the foundation everything else sits on:

-   **Tier 0 — declarative rebuild + backups (the foundation)**: the
    cluster *is* the Pulumi program plus Talos machine configs. Survival
    of permanent loss comes from: hourly etcd snapshots shipped to B2
    (off-provider by the storage.md §4 placement rule), VolSync volume
    backups and CNPG barman to the same bucket (storage.md §5), and
    periodically *drilled* restores. Target: RPO ≤ 1 h, RTO ~1–2 h
    hands-on. The **cold-standby drill** covers total-cloud-loss
    (tenancy termination included): bootstrap a temporary single-node CP
    on the homelab host (libvirt) from the latest etcd snapshot in
    ~30–60 min, then rebuild the cloud pool at leisure. (The scratch
    CP VM's ~4 GiB has no standing slack to come from on the 32 GB
    host — the drill script explicitly squeezes ARC / the worker VM
    for the duration and restores them after.)
-   **Tier 1 — workload HA**: apps that support replication run
    multi-replica across the pools (CNPG multi-instance, stateless ×2).
    Even under full CP loss, running workloads keep serving — kubelet
    and the Cilium datapath don't need the API to keep forwarding.
-   **Tier 2 — public-ingress HA (delivered by design)**: the NLB
    health-checks the three nodes; Envoy runs on all of them
    (architecture.md §3.2–3.3). Only hath is single-instance by
    protocol necessity (its dedicated VIP survives node loss; only the
    cache volume ties it to a node at a time).
-   **Tier 3 — control-plane HA (delivered by design)**: etcd quorum
    across the three cloud nodes, same region, same AD-or-better
    placement (spread across fault domains). Degradation ladder: lose
    1 node → quorum holds, replace at leisure (the capacity hold, §3.2);
    lose 2 → etcd disaster recovery from the surviving member
    (re-bootstrap single-member, scale back to 3 — minutes-to-an-hour of
    API downtime, no data loss); lose all 3 → Tier 0 cold standby.
    On the Vultr fallback this tier degrades to the superseded
    homelab-CP layout (architecture.md §6.5) with the drill direction
    flipped back.

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
