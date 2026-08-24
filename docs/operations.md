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
| k8s-base charts (cert-manager, CNPG, VolSync, sealed-secrets, VictoriaMetrics, …) | renovate | CI chain | Reviewed — chart bumps always produce a real diff; major behind dashboard approval |
| **Cluster images, infra and app alike** (CNPG operands, self-built, third-party app images) | renovate | CI chain (merge = deploy) | **Minor automerge** (patch stream folded into minor — the legacy `patch: enabled: false` precedent); **major behind dashboard approval + review**; CNPG operand major additionally gated on the self-built image line (workloads.md §4). The safety valve is the deploy-failure alert, not a per-bump eyeball — this deliberately reverses legacy's "applications get eyeballed" stance |
| blog image / built branch | blog repo CI | git-sync | Automatic — content, not code |
| nspawn rootfs (caddy, AdGuard, ZeroTier) | renovate in homelab-containers; digest-pin PR here | gw-config provider push | Reviewed |
| State-backend pins (FCOS stream handled by Zincati; `postgres:NN` in Butane) | Zincati (periodic window) / renovate on `deploy/state-backend/` | auto / manual re-provision | state-backend.md §4 |
| Pulumi SDK + providers, Python deps, Actions versions | renovate | **noop-automerge workflow** — merges once the preview is proven empty (the zero-diff rule, ci.md) | Automerged when diff-free; a bump that produces a real diff falls out of the noop path to human review; major behind dashboard approval |
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
broken.** Corollary for a one-operator system: **a drill that needs
the operator is a drill that will eventually be skipped** — so the
default form is a scheduled automation that alerts only on failure,
and the human appears exactly where an offline secret or physical
action is irreducible.

**The enabler for the biggest drill**: pg_dumps gain a third age
recipient — an **ops-repo-held drill key** (credentials.md). This
adds no new *class* of exposure: the kluster CI already reads the
live database through its client cert, and the ops repo holding the
drill key is fenced at the same private tier (PR-only default
branch, architecture.md §4.3); the offline generations keep their
actual role, surviving the loss of GitHub itself. With it, the
state-backend rebuild drill runs unattended end to end.

| Drill | Cadence | Form |
| --- | --- | --- |
| CNPG restore (immich pattern, ported from legacy) | Monthly | Automated in-cluster, alert on failure |
| State-backend rebuild — scratch micro from Butane → restore latest dump (drill key) → verify → destroy (state-backend.md §7.3) | Quarterly | Automated (ops repo), alert on failure |
| etcd snapshot restore-verify — latest B2 snapshot into a scratch etcd, health + key sanity | Monthly | Automated (ops repo), alert on failure |
| VolSync spot-restore — rotating PVC into a scratch namespace, checksum, tear down | Monthly | Automated in-cluster, alert on failure |
| Orphan-volume audit, target zero (storage.md §3.3) | Quarterly | Automated; `actionable` alert only on findings |
| Credential expiry + destroy-date tripwires (credentials.md §4) | Continuous (scheduled probes) | Automated; `actionable` alert when a date approaches/passes |
| **Offline day**: age key rotation (proves offline custody, state-backend.md §7.4) + full cold-standby reverse bootstrap on homelab libvirt (nodes.md §5) + offline-kit verification against the register (credentials.md §2.1) + a `pulumi preview` against the Vultr-fallback stack config (nodes.md §3.1 — proves the scripted fallback still computes, creating nothing) + anything the probes can't reach | Yearly | One `actionable` issue, human-run |

Every scheduled drill above runs in the **ops repo** (ci.md §3 —
the deployment repo carries no scheduled workflows; the two
in-cluster drills, VolSync spot-restore and the CNPG restore, are
the exceptions — kube-native scratch-namespace operations driven by
the cluster itself, so the ops repo never needs a kubeconfig). Every automated drill is covered by a **freshness alert**
(the backup-freshness family, cluster-infra.md §3): a drill that
silently stops running is indistinguishable from a failing one. The only
calendar ritual left is the yearly offline-day issue.

## 5. Playbook index

The unified alert channel requires every alert to name its playbook
(architecture.md §4.3); this index is where the names resolve.
Owning docs keep the content — the index only locates it.

| Playbook family | Lives in |
| --- | --- |
| State backend (cert/CA, PG major, rebuild, age rotation) | physical/state-backend.md §7 |
| Gateway (ZT container down, firmware-wiped estate, UDM replacement) | physical/gateway.md §3 |
| Node replacement (CP node, worker VM, augmented-node extras) | §3 here |
| Upgrades (Talos serial, Cilium canary) | §2 here |
| Backup restores (CNPG, VolSync, etcd) | storage.md §5 + drill scripts |
| Alert-channel failure (HA push down → meta-alert; GitHub leg down) | architecture.md §4.3 |

vmalert rule families adopt this index as they are ported: an alert
that cannot point at a row (or at its owner doc's census) does not
ship — the same-change rule.
