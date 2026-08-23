# Declarative Design: Workloads (the `apps` Stack)

The per-app pattern: one Python component per application, declaring
everything the app needs — workload, storage, exposure, DNS, secrets,
backups, placement — so that "deploy an app" is one reviewable diff in
one place. This is the top stack of
[framework/pulumi.md](../framework/pulumi.md) §3 and the home of ~80–90%
of all future changes.

> **Status**: designed 2026-08-22. Not implemented.

## 1. The component contract

Every app is a `Component` subclass (putils, RFC-001) that owns:

| Concern | Declared as | Governed by |
| --- | --- | --- |
| Namespace | created by the component (never shared) | pulumi.md §3 |
| Workload | Deployment/StatefulSet with **honest requests/limits** — CPU limits are mandatory on anything scheduled to the cloud pool (etcd shares those cores, nodes.md §1); memory requests sized from evidence, not idle numbers (the JuiceFS-sidecar lesson, storage.md §6) | nodes.md §§1, 4.4 |
| Storage | per the decision rules: object-direct → CNPG+local-path → NAS → local-path+VolSync → (justified) JuiceFS | storage.md §2 |
| Backup | a VolSync `ReplicationSource` beside every rule-4 PVC — part of the component, not an afterthought | storage.md §3.1 |
| Exposure | pool label + route kind per the routing matrix, via the helpers (`public_route`/`public_port`/`lan_route`), which also emit the NLB listener + security rule for internet ports | architecture.md §3.6, dns.md §3 |
| DNS | emitted by the same helpers (CNAME to anchor; AdGuard rewrite for split-horizon) | dns.md |
| Secrets | SealedSecret first choice, `template.data` pattern | cluster-infra.md §1.1 |
| Placement | scheduling constraints only: site pool (cloud/homelab), the augmented node for dedicated-VIP workloads, GPU resource requests | architecture.md §3.6 |
| Network policy | per-namespace default-deny + explicit allows, part of the component | architecture.md §4.1 |
| Monitoring | scrape/dashboard labels per the legacy conventions (`release`, `grafana_dashboard`) so VictoriaMetrics/grafana pick them up | cluster-infra.md §1 |

What a component may **not** do: hostPort, hostNetwork, `externalIPs`
(architecture.md §6.6); cross-namespace reach-ins; unbudgeted sidecars;
cluster-scoped resources (those belong to `k8s-base` and go through its
closed-list rule).

## 2. Shaped patterns (the non-trivial apps)

-   **Dedicated-VIP workload (hath)**: LB Service requesting the
    dedicated VIP + a `CiliumEgressGatewayPolicy` with `egressIP` = the
    secondary private IP + node affinity to the augmented node (cache
    volume locality) + strict CPU limits. All four pieces in the one
    component (architecture.md §3.2).
-   **Split-horizon app (immich)**: one set of pods, two exposures
    (`public_route` to both gateways) — the helper emits both routes,
    the public CNAME, and the LAN rewrite. The immich LAN-direct rule
    (never via the cloud path) is thereby structural.
-   **Bulk-egress (qbittorrent + seedwatch)**: pinned to the homelab
    pool; outbound v6 via the cluster masquerade, inbound v4 via the
    existing UDM forward, inbound v6 pinhole declared as a unifi
    firewall rule *in this component* (co-location again); seedwatch in
    the same namespace, talking to the qbittorrent Service and the NAS
    hardlink paths.
-   **JuiceFS-quarantined app (VPS-successor syncthing + dav)**:
    in-pod juicefs mount (sidecar, no CSI) with 0.5–1 GiB requests,
    SQLite metadata on the cloud node's volume, its own OCI bucket
    (from physical outputs), metadata auto-backup to the bucket
    (storage.md §6).
-   **CNPG-backed app (immich, splitpro, …)**: CNPG `Cluster` on
    local-path + barman to B2, monthly restore drill inherited from the
    legacy discipline (storage.md §5).

## 3. Porting from kluster-code

Most apps port as a rewrite of their kluster-code component onto this
contract — same images, same SealedSecrets (after the sealing-key
restore, cluster-infra.md §1), new exposure/storage declarations. The
migration order and data movement are migration.md's concern; the shape
each app lands in is this document's.
