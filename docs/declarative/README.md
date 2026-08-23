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
    UDM, B2, bootstrap order + verification checklist.
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
