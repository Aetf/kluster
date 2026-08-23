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
    UDM, DNS anchors, buckets, bootstrap order + verification
    checklist.
-   **dns.md** — all public DNS in Pulumi (absorbing the DNSControl
    repo): zone + NLB anchor records in `physical`, per-app records
    declared next to each app in `apps` (decided 2026-08-22 — no
    dedicated DNS layer), split-horizon rewrites toward AdGuard.
-   **cluster-infra.md** — in-cluster foundations: Cilium (LB pools, BGP
    peering, Gateway API), VolSync, cert-manager, CNPG operator,
    monitoring (VictoriaMetrics + grafana) — their install order and the
    bootstrap-dependency rules.
-   **workloads.md** — the per-app pattern: how an app declares its
    pool/route (cluster/architecture.md §3.6), storage class
    (cluster/storage.md §2), placement, secrets, and backups in one
    component.
