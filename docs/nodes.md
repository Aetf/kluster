# Node & Provider Selection

Objective: Pick the concrete machines for the architecture fixed in
[architecture.md](architecture.md) — which cloud provider and instance for the
internet-facing worker, how the Homelab VM is sized, and what "high
availability" means for a two-site, budget-capped cluster. Price-driven;
baseline to beat is the legacy Vultr VPS at $30/mo all-in.

> **Status**: Reviewed interactively 2026-08-21; decisions marked below.
> Cloud provider is **tentatively Hetzner CPX21** (final call after a
> total-cost check before commit). All prices are as-of August 2026, USD,
> and must be re-verified in the provider calculators before commit
> (GCP CUDs and Hetzner in particular changed prices in 2026). Nothing here
> is implemented yet.

## 1. What the cloud node actually does

Per architecture.md the cloud node is a **worker only**: Cilium Gateway
API (Envoy, hostNetwork 80/443), raw TCP/UDP service ports, KubeSpan
endpoint, hath, and whatever else is pinned to the internet pool. The control plane stays in the Homelab. This
means the cloud node needs:

-   Modest CPU/RAM: Envoy + Cilium + a handful of pods. 4 GB RAM is the
    comfortable floor (2 GB is possible but leaves no headroom for Cilium +
    Envoy + kubelet + workload bursts); 2 vCPU.
-   A public IPv4 **and** IPv6 address (dual-stack requirement).
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
| GCP Standard tier              | 200 GiB/mo           | $0.085/GB      |
| AWS EC2                        | 100 GB/mo            | $0.09/GB       |
| Hetzner Cloud (US)             | 1 TB/mo              | ~$1–2/TB       |
| Vultr (legacy baseline)        | 2–4 TB/mo (plan)     | $0.01/GB       |

Two provider-specific traps:

1.  **GCP: external IPv6 requires Premium tier.** The 200 GiB free
    allowance and cheaper rates are Standard-tier only, and Standard tier
    does not support external IPv6 (regional or global). A dual-stack GCP
    node therefore pays Premium egress from the second GiB. (A mixed setup —
    IPv4 access config on Standard tier + IPv6 on Premium — may be
    configurable per-address; worth one experiment during implementation,
    but do not design around it until proven.)
2.  **AWS Lightsail is disqualified** despite its bundled 1–3 TB transfer:
    it cannot boot custom images, so no Talos.

### 2.1 Measured traffic (legacy cluster Prometheus, 30d to 2026-08-21)

The legacy VPS NIC moved 1276 GB TX/30d, but decomposed:

| Component | GB/30d | Fate in the new cluster |
| --- | --- | --- |
| ZeroTier tunnel encap (to homelab) | ~780 | becomes KubeSpan; free on Hetzner (1 TB incl.), billed on GCP/AWS |
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
    GB/mo fits trivially inside Hetzner's 1 TB allowance, and its 50 Gi
    cache fits the CPX21's 80 GB disk.

## 3. Provider comparison (cloud worker, dual-stack, Talos)

Monthly cost = instance + boot disk + IPv4 + egress at three traffic points.
Instance prices are 1-year committed/reserved where available (3-year rates
are ~30% lower still; defer committing until the design has run for a few
months).

| | GCP e2-medium | AWS t4g.medium | Hetzner CPX21 (US) | Vultr 4 GB |
| --- | --- | --- | --- | --- |
| Specs | 2 shared vCPU / 4 GB | 2 vCPU ARM / 4 GB | 3 vCPU / 4 GB | 2 vCPU / 4 GB |
| Instance (1y commit) | $15.41 | ~$16 | ~$13 (no commit) | ~$24 (no commit) |
| Boot disk 50 GB | $5.00 (pd-balanced) | $4.00 (gp3) | included (80 GB) | included |
| Public IPv4 | $3.65 | $3.65 | ~$0.60 | included |
| Fixed subtotal | **$24.06** | **$23.65** | **~$13.60** | **~$24** |
| @ 50 GB egress | $29.94 | $23.65 | $13.60 | $24 |
| @ 300 GB egress | $59.94 | $41.65 | $13.60 | $24 |
| @ 1 TB egress | ~$144 | ~$105 | $13.60 | $24 |
| Talos support | image upload | AMI upload | ISO/snapshot | custom ISO |
| Dual-stack | Premium tier, custom-mode VPC, /96 per VM | native, free IPv6 | native, /64 free | native |
| CUD/RI escape hatch | 1y/3y CUD | 1y/3y RI | none needed | none needed |
| Notes | t2a ARM has **no CUD** — skip; e2 is shared-core burst | 100 GB/mo egress free | CPX only in US (no CX/CAX); 1 TB incl.; 2026 price hikes | legacy incumbent |

Smaller variants if the node stays ingress-only: GCP e2-small (2 GB, $7.70
1y CUD), AWS t4g.small (2 GB, ~$8 1y RI). Same fixed adders apply.

### 3.1 Decision (2026-08-21, tentative)

Measured steady-state egress (~150–220 GB/mo, §2.1) settles it against the
hyperscalers: at these volumes GCP lands at ~$42–50/mo (dual-stack forces
Premium-tier egress), AWS ~$33/mo, **Hetzner CPX21 (Ashburn) ~$14/mo flat**
— and Hetzner's allowance also absorbs KubeSpan inter-node traffic that
GCP/AWS would meter. No managed cloud service is consumed by this
architecture, so the hyperscaler premium buys nothing; pulumi-hcloud is
mature and Talos installs via ISO/snapshot.

**Tentatively selected: Hetzner CPX21, Ashburn.** Final confirmation after
a total-cost pass at the end of detailed design; AWS t4g.medium is the
named fallback if Hetzner proves operationally inadequate (see
architecture.md §6.3). No 1y/3y commitment applies to Hetzner, which also
removes the commitment-timing question.

## 4. Homelab node(s)

The Homelab side runs on the existing physical server under libvirt
(pulumi-libvirt, macvlan to LAN per architecture.md §5).

-   **CP/worker VM** (the `192.168.80.238` VM): 4 vCPU / 16 GB / 100 GB, disk
    on local NVMe. etcd wants <10 ms fsync — keep its disk off any network
    or USB storage, and out of any qcow2-on-JuiceFS arrangement.
-   **Optional second worker VM** (2–4 vCPU / 8 GB): not required by the
    architecture, but it is the cheapest way to make Longhorn `replica=2`
    possible *within* the site (see storage.md §3) and to drain the main VM
    for maintenance without evicting everything to the cloud node. RAM on
    the physical host is the only cost. **Decision 2026-08-21: not in the
    initial build** — adding a Talos node later is cheap; until then every
    Longhorn volume runs replica=1 + S3 backups. The design must not assume
    it exists.

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
