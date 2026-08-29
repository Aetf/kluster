# Declarative Design

How each layer of the system is *declared* in the Pulumi program — the
stack decomposition, per-layer resource models, and the boundaries
between them. Distinct from [cluster/](../cluster/) (what we're building
and why) and [framework/](../framework/) (the Python machinery it is
written with — how a stack program is dispatched and how a value
mechanically crosses a stack boundary is
[framework/pulumi.md](../framework/pulumi.md) §3).

## 1. The stacks

> **Status**: decided 2026-08-22 (interactive review; `dns` added the
> same day). The physical layer is one stack across both sites, not one
> per site: the two sites change at the same rate and reference each
> other, so a split by site would buy nothing and cost a boundary.

Five stacks in one project, one environment: the four CI deploys, plus
`github`, which declares the forge those four are deployed by and is
applied by hand.

| Stack | What it owns | Change cadence |
| --- | --- | --- |
| `physical` | everything that must exist before the Kubernetes API does | low, roughly monthly |
| `dns` | zones, the anchors, and the estate records no app owns | low, and independent of the cluster |
| `k8s-base` | everything cluster-scoped that speaks the Kubernetes API | medium, mostly chart bumps |
| `apps` | every application, its namespace, storage, exposure and records | high — the daily driver |
| `github` | the repositories, their Environments and gates, branch protection | lowest, and the only stack CI does not apply |

Boundary rules:

-   **The only hard boundary is "exists before the Kubernetes API
    does"** — `physical` against the rest. The base/apps split is a
    blast-radius and preview-hygiene boundary instead: a Cilium upgrade
    can never ride along inside an application deploy, and an
    application preview does not load the operator machinery. It pays
    for itself on frequency, since roughly 80–90% of all ups touch
    `apps` and nothing else.
-   **Namespaces belong to applications.** Each application component
    creates its own namespace; `k8s-base` owns only shared,
    cluster-scoped infrastructure.
-   **Deploy order is CI's job, not Pulumi's.** `apps` needs the
    operators and CRDs `k8s-base` installs, and no Pulumi construct
    expresses a dependency across stacks, so the pipeline orders the
    applies ([framework/ci.md](../framework/ci.md)).

## 2. What crosses a stack boundary

**Conventions are code, not stack outputs.** Gateway names, pool
labels, storage-class names and the ZeroTier roster live in a shared
`conventions` package that every program imports, and the singletons it
names have autonaming disabled, so the literal in the module is the real
name ([cluster-infra.md](cluster-infra.md) §0). A table two stacks
decide from belongs there even when only one of them declares resources
for it: `physical` admits overlay members by the roster while `dns`
publishes the `*.zt` host block from the same roster, so the table is
stated once rather than imported across a package boundary.

A `StackReference` therefore carries only machine facts — values no
program can know until an apply produces them. Nearly all of them are
the `physical` stack's outputs, enumerated in
[physical.md](physical.md) §0; the exception is `dns`'s zone IDs, which
`apps` consumes for the records it declares beside each application
([dns.md](dns.md) §1). A resource that deliberately keeps autonaming
publishes its generated name the same way, because a generated name is
a machine fact too.

## 3. The documents

Created as each area reaches detailed design:

-   **[physical.md](physical.md)** (written) — the `physical` stack:
    OCI resources + Talos day-1 via pulumiverse-talos (day-2 stays
    talosctl), libvirt worker VM + adopted HAOS, gw-config/unifi on the
    UDM, B2, bootstrap order + verification checklist. The physical
    *systems* it declares are designed in
    [../physical/](../physical/) (state-backend appliance, homelab
    host & VM) — this doc stays about how they're declared.
-   **[dns.md](dns.md)** (written) — the `dns` stack: zones + estate
    records + anchors in `dns`, per-app CNAMEs in `apps` (zone sets
    replace the alias-domain copy-paste), split-horizon via direct
    dual-write to both AdGuard instances (adguardhome-sync retires),
    one-line helpers; DNSControl repo absorbed and retired.
-   **[cluster-infra.md](cluster-infra.md)** (written) — the `k8s-base`
    stack: closed component list (Gateway API CRDs → Cilium →
    sealed-secrets → cert-manager → CNPG/VolSync → VictoriaMetrics →
    NFD/GPU plugin), secrets placement rules, the full Cilium
    configuration (pools, BGP,
    gateways, EGW), and what the stack deliberately does not do.
-   **[workloads.md](workloads.md)** (written) — the per-app component
    contract (workload/storage/backup/exposure/DNS/secrets/placement/
    policy/monitoring in one place), the shaped patterns (dedicated-VIP,
    split-horizon, bulk-egress, JuiceFS-quarantined, CNPG), and the
    porting rule from kluster-code.

The `github` stack has no document here: what it declares is designed
alongside the forge it configures, in
[framework/github.md](../framework/github.md) §3 — and what it
deliberately leaves as console state, audited rather than declared, in
that document's §4.

## 4. Deliberately not pre-decided

Settled on first contact, in this order of appearance, because deciding
them earlier would be deciding them on no evidence:

-   **Version pins** for Talos, Cilium and the charts — renovate takes
    over after the first pin. Known floors: **Cilium ≥1.20** for the
    ExternalAuth route filter, ≥1.16 for tunnel-mode Egress Gateway,
    Longhorn ≥1.12 if it is ever adopted.
-   **Alertmanager routing details** beyond "ported from legacy"
    (operations.md §4 fixes the alert contract, not the routing tree).
