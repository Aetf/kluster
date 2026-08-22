# Declarative Design

How each layer of the system is *declared* in the Pulumi program — the
stack/layer decomposition, per-layer resource models, and the boundaries
between them. Distinct from [cluster/](../cluster/) (what we're building
and why) and [framework/](../framework/) (the Python machinery it's
written with). Layering follows the proposal in
[framework/pulumi.md](../framework/pulumi.md) §3.

Planned documents (created as each area reaches detailed design):

-   **physical.md** — the physical layer: cloud instance + IPs/firewall,
    libvirt VMs on the homelab host (the Talos VM *and* the adopted HAOS
    domain, cluster/architecture.md §5.1/§6.8), Talos machine configs,
    and the `gw-config` dynamic provider driving the UDM (FRR/BGP,
    firewall pinholes — cluster/architecture.md §5.2).
-   **dns.md** — all public DNS in Pulumi (absorbing the DNSControl
    repo), split-horizon rewrites toward AdGuard, and how records derive
    from Service/Gateway resources instead of being hand-listed.
-   **cluster-infra.md** — in-cluster foundations: Cilium (LB pools, BGP
    peering, Gateway API), Longhorn, cert-manager, CNPG operator,
    monitoring — their install order and the bootstrap-dependency rules.
-   **workloads.md** — the per-app pattern: how an app declares its
    pool/route (cluster/architecture.md §3.6), storage class
    (cluster/storage.md §2), placement, secrets, and backups in one
    component.
