# Day-2 Operations

Who and what keeps the system current and its recovery paths proven:
the update-ownership matrix, the upgrade and node-replacement
runbooks, and the drill program with its trigger machinery. Sits at
the docs root like [credentials.md](credentials.md) — day-2 crosses
every layer boundary. Runbooks here follow the census discipline
(title, trigger, gist; executable form — `just` recipes and scripts —
ships with the implementation).

## 1. Update ownership matrix

**Rule: a pin without a PR-opener is drift.** Every pinned artifact
maps to a renovate (or equivalent) opener and a named apply path;
this matrix is the census. "Reviewed" means a human merges after
reading the preview; nothing on this table automerges unless the row
says so.

| Surface | PR opened by | Applied by | Policy |
| --- | --- | --- | --- |
| Talos version (machine-config pin + Image Factory schematic) | renovate (GitHub-releases datasource) | `just` task wrapping `talosctl upgrade`, serial, staged (physical.md §5) | Reviewed; §2.1 runbook |
| Kubernetes version | same PR family (Talos-coupled) | `talosctl upgrade-k8s` | Reviewed; after the Talos bump it belongs to |
| Cilium chart | renovate | CI chain (merge = deploy) | Reviewed, **never automerged** — §2.2 runbook; ≥1.20 floor (ExternalAuth) |
| k8s-base charts (cert-manager, CNPG, VolSync, sealed-secrets, VictoriaMetrics, …) | renovate | CI chain | Reviewed; patch bumps with zero-object preview diffs may automerge (noop rule) |
| CNPG operand images (self-built) | renovate (base + PG minor) | CI chain | Minor reviewed; **major never automerged**, gated on the self-built image line (workloads.md §4) |
| Self-built app images (emailproxy, golinks) | renovate | CI chain via digest pin | Reviewed |
| blog image / built branch | blog repo CI | git-sync | Automatic — content, not code |
| nspawn rootfs (caddy, AdGuard, ZeroTier) | renovate in homelab-containers; digest-pin PR here | gw-config provider push | Reviewed |
| State-backend pins (FCOS stream handled by Zincati; `postgres:NN` in Butane) | Zincati (periodic window) / renovate on `deploy/state-backend/` | auto / manual re-provision | state-backend.md §4 |
| Pulumi SDK + providers, Python deps, Actions versions | renovate | CI chain / repo tooling | Reviewed; provider bumps judged by preview diff |
| UDM firmware | **vendor-controlled** (auto-update schedule; outage history on record) | — | Not ours to pin; the estate self-heals via on_boot.d, ZT recovery runbook gateway.md §3 |

## 2. Upgrade runbooks (census)

-   **§2.1 Talos node upgrade.** Trigger: version-pin PR merged.
    Gist: one cloud node first as canary (health + workload
    settle), then the remaining nodes serially — never two quorum
    members at once (the CI-serialization rule, physical.md §5);
    homelab worker last; `upgrade-k8s` afterwards as its own step.
-   **§2.2 Cilium upgrade.** Trigger: chart PR. Gist: the riskiest
    bump in the system — before merge, re-run the affected subset of
    the bootstrap verifications on the preview environment of one
    node (LB-IPAM node-IP pools, EGW + reserved-IP NAT, MTU over
    KubeSpan, ExternalAuth fail-closed); merge deploys; watch the
    Envoy/agent rollout complete before calling it done.
-   **§2.3 State-backend lifecycle** — owned by
    physical/state-backend.md §7 (pointer, not a copy).
-   **§2.4 CNPG major upgrade** — owned by workloads.md §4
    (pointer).

## 3. Node replacement runbooks (census)

-   **§3.1 Cloud CP node.** Trigger: hardware loss, A1 reclamation,
    or deliberate rebuild. Gist: drain → `talosctl etcd
    remove-member` → destroy in `physical` (unprotect if flagged) →
    re-create → rejoin quorum → health gate. Facts that shape it:
    A1 capacity at re-create is the known risk (the standing quorum
    holds capacity; rebuild promptly), NLB backends follow from
    `physical` automatically; the **augmented node** carries extra
    steps — block volume reattach, secondary private IP, reserved-IP
    NAT re-point (architecture.md §3.2).
-   **§3.2 Homelab worker VM.** Trigger: VM/disk loss or rebuild.
    Gist: re-create from machine config (nocloud seed); local-path
    data returns via VolSync restore (that's the drilled move-path,
    storage.md §2); NAS-backed PVs re-point untouched; BGP session
    re-establishes from the static IP in the config.
-   **§3.3 Total-cloud-loss / total-home-loss** — owned by nodes.md
    §5 (cold-standby drill, both directions).

## 4. Drill program

Principle (standing): **an undrilled recovery path is assumed
broken.** The machinery triggers; the operator executes.

**Trigger mechanism**: a quarterly scheduled workflow in CI raises an
`actionable` alert through the unified channel (architecture.md §4.3)
— so the drill lands as an HA push *and* a kluster-alerts issue whose
body is the quarter's checklist with playbook links. The issue stays
open until every item is checked; closing it is the completion
record. No human has to remember the calendar — forgetting requires
ignoring an open issue.

| Drill | Cadence | Form |
| --- | --- | --- |
| CNPG restore (immich pattern, ported from legacy) | Monthly | Fully automated, alerts only on failure |
| State-backend rebuild (covers DR + PG major + cert delivery, state-backend.md §7.4) | Quarterly | Scripted, human-run (offline age key) |
| etcd cold-standby bootstrap (nodes.md §5; alternate directions) | Quarterly | Scripted, human-run |
| VolSync spot-restore (one PVC, rotating pick) | Quarterly | Scripted, human-run |
| Orphan-volume audit, target zero (storage.md §3.3) | Quarterly | Scripted check, human review |
| Credential-register audit (credentials.md §4) | Quarterly | Checklist, human-run |
| age key rotation (state-backend.md §7.5) | Yearly | Scripted, human-run (offline) |

## 5. Playbook index

The unified alert channel requires every alert to name its playbook
(architecture.md §4.3); this index is where the names resolve.
Owning docs keep the content — the index only locates it.

| Playbook family | Lives in |
| --- | --- |
| State backend (cert/CA, NSG refresh, PG major, rebuild, age rotation) | physical/state-backend.md §7 |
| Gateway (ZT container down, firmware-wiped estate, UDM replacement) | physical/gateway.md §3 |
| Node replacement (CP node, worker VM, augmented-node extras) | §3 here |
| Upgrades (Talos serial, Cilium canary) | §2 here |
| Backup restores (CNPG, VolSync, etcd) | storage.md §5 + drill scripts |
| Alert-channel failure (HA push down → meta-alert; GitHub leg down) | architecture.md §4.3 |

vmalert rule families adopt this index as they are ported: an alert
that cannot point at a row (or at its owner doc's census) does not
ship — the same-change rule.
