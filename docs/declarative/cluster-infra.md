# Declarative Design: the `k8s-base` Stack

How the in-cluster foundations are declared — the component set, its
install/dependency order, and the configuration points already decided
elsewhere (architecture.md for the network design, storage.md for
storage, nodes.md §4.4 for why this list is as small as it is). This is
the middle stack of [framework/pulumi.md](../framework/pulumi.md) §3:
everything cluster-scoped that speaks the k8s API, consumed by `apps`.

> **Status**: designed 2026-08-22. Not implemented.

## 0. Scope and rules

-   Inputs (StackReference to `physical`): kubeconfig, node
    primary/private IPs, the augmented node's secondary private IP, the
    NLB IP. Everything else apps need from this stack (gateway names,
    pool labels, storage-class names) is shared as code in
    `conventions.py`, not as outputs.
-   **The component list is closed.** Every entry below pays standing
    rent (nodes.md §4.4); additions require the same justification in
    writing. Notably absent, on purpose: Longhorn (storage.md §3.2),
    JuiceFS CSI (storage.md §6), external-dns (architecture.md §6.4),
    prometheus-operator (replaced by VictoriaMetrics).
-   Namespaces belong to app components (`apps` stack); this stack
    creates only the namespaces of its own components.

## 1. Install order

The order is a real dependency chain, encoded as Pulumi
`depends_on`/parent relationships so a single `up` converges from an
empty cluster:

1.  **Gateway API CRDs** — must exist before Cilium starts its gateway
    controller.
2.  **Cilium** (§2) — the cluster has no CNI until this lands (Talos is
    configured `cni: none`; nodes sit NotReady between physical
    bootstrap and this step, which is fine — CI runs the stacks
    back-to-back, and nothing else can schedule anyway).
3.  **cert-manager** — ACME with Cloudflare DNS-01 (token is a stack
    secret); every later component may reference issuers.
4.  **sealed-secrets controller** — the secret-management model is
    unchanged from kluster-code. Migration note: the legacy sealing key
    is restored into the new cluster *before* any legacy SealedSecret
    manifests are ported, or everything gets re-sealed
    (migration.md).
5.  **CNPG operator**, **VolSync** — independent of each other; both
    before any app declares a database or a backup schedule.
6.  **Monitoring**: VictoriaMetrics (vmsingle + vmagent + vmalert) +
    grafana, PromQL-compatible replacement for the legacy
    prometheus-operator stack at ~1/5 the RAM (nodes.md §4.4); scrape
    configs and alert rules follow the legacy label conventions so
    dashboards port over.
7.  **NFD + Intel GPU device plugin** — inert until the GPU cutover
    flips vfio on the homelab worker (physical.md §3), present from
    day 0 so the cutover needs no k8s-base change.

`packages/crds` is regenerated (`uv run update_crds`) against exactly
this chart set; the legacy chart list retires with kluster-code.

## 2. Cilium: the load-bearing component

All decided behavior from architecture.md §3, expressed as config:

-   **Datapath**: kube-proxy replacement on; `k8sServiceHost:
    localhost`, `k8sServicePort: 7445` (KubePrism — mandatory, there is
    no kube-proxy to fall back on); dual-stack with IPv4 primary;
    IPv6 masquerade on (qbittorrent's egress path, §3.5); MTU sized for
    the KubeSpan underlay (WireGuard overhead — verify, don't assume).
-   **LB IPAM**: two `CiliumLoadBalancerIPPool`s — `internet` (the
    three node primary IPs + the augmented node's secondary private IP,
    all from physical outputs) and `lan` (`192.168.70.0/24` + the ULA
    /64). Pool membership via the `serviceSelector` label from
    `conventions.py`. Bootstrap verification: pool-contains-node-IP.
-   **BGP**: `CiliumBGPPeeringPolicy` on the homelab worker only,
    peering with the UDM (AS 65000) over both families, advertising
    `lan` VIPs as /32 + /128. The UDM side is physical's gw-config
    provider; the session only establishes once both stacks are up —
    acceptable, nothing LAN-facing exists before apps deploy.
-   **Gateway API**: enabled; two `Gateway`s — `internet-gw` (Envoy
    replicas across the cloud nodes, `externalTrafficPolicy: Local`,
    Service requesting all three primary IPs via `lbipam.cilium.io/ips`
    + sharing-key) and `lan-gw` (pinned to the homelab worker, `lan`
    pool). Apps attach `HTTPRoute`s (§3.6 matrix).
-   **Egress Gateway**: enabled (the dedicated-VIP pattern's outbound
    half, architecture.md §3.2); the `CiliumEgressGatewayPolicy`
    instances themselves belong to the workloads that need them
    (`apps`). Bootstrap verification: EGW under the chosen routing
    mode.
-   **Hubble**: enabled with relay + UI off by default (metrics into
    VictoriaMetrics; the UI is a port-forward away when needed — no
    standing dashboard, per the standing-rent rule).
-   **Network policy stance** (architecture.md §4.1): default-deny is a
    per-namespace app concern; this stack ships only the cluster-wide
    baseline (blocking pod→management-plane except where declared).

## 3. What this stack deliberately does not do

-   No ingress of its own — gateways are wiring; routes/listeners/
    security-rules arrive with apps (the derived-not-enumerated
    principle, physical.md §1).
-   No app namespaces, quotas, or per-app policy.
-   No backup schedules — VolSync `ReplicationSource`s are declared
    beside their PVCs in `apps` (storage.md §3.1); this stack only
    installs the controller and the shared B2 restic secret material.
-   No DNS records (declarative/dns.md).
