# Declarative Design

How each layer of the system is *declared* in the Pulumi program — the
stack/layer decomposition, per-layer resource models, and the boundaries
between them. Distinct from [cluster/](../cluster/) (what we're building
and why) and [framework/](../framework/) (the Python machinery it's
written with). Layering follows the proposal in
[framework/pulumi.md](../framework/pulumi.md) §3.

Documents (created as each area reaches detailed design):

-   **[physical.md](physical.md)** (written) — the `physical` stack:
    OCI resources + Talos day-1 via pulumiverse-talos (day-2 stays
    talosctl), libvirt worker VM + adopted HAOS, gw-config/unifi on the
    UDM, B2, bootstrap order + verification checklist. The physical
    *systems* it declares are designed in
    [../physical/](../physical/) (state-backend appliance, homelab
    host & VM) — this doc stays about how they're declared.
-   **[dns.md](dns.md)** (written) — the fourth stack: zones + estate
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

## Deliberately not pre-decided

Settled on first contact, in this order of appearance, because deciding
them earlier would be deciding them on no evidence:

-   **Version pins** for Talos, Cilium and the charts — renovate takes
    over after the first pin. Known floors: **Cilium ≥1.20** for the
    ExternalAuth route filter, ≥1.16 for tunnel-mode Egress Gateway,
    Longhorn ≥1.12 if it is ever adopted.
-   **The AdGuard static-config templating shape** inside the gw-config
    estate (dns.md §3 fixes what the rewrites are, not how the static
    halves are rendered).
-   **Alertmanager routing details** beyond "ported from legacy"
    (operations.md §4 fixes the alert contract, not the routing tree).
