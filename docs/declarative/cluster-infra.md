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
    NLB IP.
-   **Names: explicit for shared singletons, outputs for the dynamic.**
    Cross-stack-referenced singletons (StorageClasses, Gateways, pools,
    shared Secret names) get explicit `metadata.name`s with autonaming
    disabled — they are well-known singletons where autonaming only
    hurts (the legacy autonamed-PVC lesson) — and those fixed names live
    in `conventions.py`. Where autonaming is deliberately kept, the
    generated name is a machine fact and flows as a stack output. The
    point of minimizing outputs is CI: every cross-stack output widens
    the "stale downstream preview" window (ci.md §3).
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

1.  **Gateway API CRDs — the experimental channel** (2026-08-24: the
    ExternalAuth HTTPRoute filter, GEP-1494, ships in experimental,
    not standard — §2) — must exist before Cilium starts its gateway
    controller.
2.  **Cilium** (§2) — the cluster has no CNI until this lands (Talos is
    configured `cni: none`; nodes sit NotReady between physical
    bootstrap and this step, which is fine — CI runs the stacks
    back-to-back, and nothing else can schedule anyway).
3.  **sealed-secrets controller** — fully self-contained (generates its
    own key pair), so it comes right after the CNI and every later
    component's credentials can be SealedSecrets (§1.1). Migration
    note: the legacy sealing key is restored into the new cluster
    *before* any legacy SealedSecret manifests are ported, or
    everything gets re-sealed (migration.md).
4.  **cert-manager** — ACME with Cloudflare DNS-01; the solver
    credential is a SealedSecret per §1.1 (a *separate*,
    minimally-scoped token from the one the pulumi-cloudflare provider
    uses); every later component may reference issuers.
5.  **CNPG operator** (≥1.26 — the floor for declarative offline
    in-place major upgrades, workloads.md §4), **VolSync** —
    independent of each other; both before any app declares a
    database or a backup schedule.
6.  **Monitoring**: VictoriaMetrics (vmsingle + vmagent + vmalert) +
    grafana, PromQL-compatible replacement for the legacy
    prometheus-operator stack at ~1/5 the RAM (nodes.md §4.4); scrape
    configs and alert rules follow the legacy label conventions so
    dashboards port over. **Alert delivery**: vmalert needs an
    Alertmanager-compatible sink — one small alertmanager instance,
    its routing ported from the legacy config (Home Assistant push),
    stays in the stack (~70 Mi, earns its rent as the alerting
    spine). This is the **in-cluster half of the unified alert
    channel** (architecture.md §4.3): same payload convention as the
    CI-origin alerts, every alert carrying its playbook reference.
    Alertmanager's one receiver is the HA webhook — it holds **no
    GitHub credential**; the GitHub-issue leg is *pulled* by the
    ops repo's poller reading alertmanager's API through a
    **dedicated header-match route** at the internet gateway
    (2026-08-24): the HTTPRoute forwards only `method: GET` + path
    `/api/v2/alerts` + an exact `Authorization: Bearer <token>`
    header match — anything else 404s. **Read-only by method
    match**, no auth middleware involved. Accepted and recorded:
    the token literal sits inside the HTTPRoute spec (a Pulumi
    config secret at render time, but readable through the k8s
    API) — tolerable for an alert-list read token; the recorded
    alternative, if that ever bothers, is Authelia OAuth2
    client_credentials + ExternalAuth bearer validation.
    HA-delivery failure surfaces as a meta-alert on the
    notification-failure metric.
7.  **NFD + Intel GPU device plugin** — inert until the GPU cutover
    flips vfio on the homelab worker (physical/homelab-host.md §3), present from
    day 0, so the cutover needs no k8s-base change.
8.  **The small standing set the legacy cluster already proved**
    (nodes.md §4.4 counted them as "kept as-is" but this list never
    named them — explicit now so the closed list is honest):
    **local-path-provisioner** (Talos ships no default StorageClass;
    its backing directory is a machine-config mount, physical.md §2),
    **metrics-server**, **reloader**. Monitoring internals
    (kube-state-metrics, the node-exporter DaemonSet) count as part of
    the VictoriaMetrics entry. None has an ordering constraint beyond
    Cilium.

`packages/crds` is regenerated (`uv run update_crds`) against exactly
this chart set; the legacy chart list retires with kluster-code. The
register the regeneration reads — every chart, its repository, its
pinned version, and the floor that pin has to clear — is
`src/kluster/scripts/update_crds/pins.py`, and the pins there are the
same ones the stack's `chart:` config carries. Rendering is **offline**:
a pinned Helm 3 binary renders each chart and the CRDs are filtered out
of the result, so the bindings describe the pinned chart set rather
than whatever some cluster happens to have installed. Two consequences
worth naming. Cilium's chart contains no CRD at all — the agent
registers its own at runtime — so its definitions are read from the
checked-in YAML at the matching release tag, and the tag and the chart
version move together. And the VictoriaMetrics stack installs only
`operator.victoriametrics.com`: this cluster has no `ServiceMonitor`
and no `PodMonitor`, so a scrape target is declared as the
VictoriaMetrics object, never as a prometheus-operator one.
Because the render never contacts a cluster, it cannot prove the set is
*complete* — a chart that creates a definition at runtime the way Cilium
does would simply be missing. The first live `up` is what proves it.

### 1.1 Secrets placement rules

Two channels, chosen by who consumes the secret:

-   **Consumed in-cluster as a k8s Secret → SealedSecret, first
    choice** — the kluster-code model carries over unchanged (including
    the `template.data` pattern: plaintext config stays reviewable in
    git, only the sensitive fields are sealed). Examples: cert-manager's
    DNS-01 token, app credentials, VolSync restic passwords, CNPG user
    secrets.
-   **Consumed by a Pulumi provider itself → Pulumi config secret**
    (passphrase-encrypted in state) — the only cases where SealedSecret
    is impossible, because the consumer is not the cluster (or the
    cluster doesn't exist yet): OCI credentials, the pulumi-cloudflare
    provider token, B2 management keys, the UDM SSH key, and the
    ZeroTier Central token — which `credentials derived zerotier record`
    delivers here from Central's own web console, as broad as the account
    it belongs to because Central publishes no token API and offers no
    narrower scope (credentials.md §3). CI's own ZeroTier *member
    identities* are not in this channel: `physical` generates them into
    state, and they reach a job as an Environment secret.
-   Where one external service serves several consumers (Cloudflare),
    issue **separately-scoped tokens per consumer**: one per channel
    above, plus a third, zone-limited token for the UDM caddy's own
    ACME issuance (dns.md §4) delivered as a gw-config device secret.

### 1.2 Installing a chart: `helm.v4.Chart`

The API every component installs through, wrapped as `kx.helm_chart`:

-   **What it is.** `helm.v4.Chart` renders the chart in the provider
    and hands each rendered object to Pulumi as its own resource. It
    does not create a Helm release, which is what `helm.sh/v3.Release`
    does instead. The trade is per-object diffs, drift remediation and
    Pulumi transforms and policies, against losing `helm list`
    visibility, release history and `helm rollback`, and the ability to
    adopt an existing release. This cluster is built from empty and
    every object in it is Pulumi's, so the losing side is empty too.
-   **OCI registries.** A chart reference may be a full `oci://` URL in
    `chart` itself, with no repository options beside it. This is the
    wall the legacy program hit: it used `helm.v3.Chart`, which cannot
    read an OCI URL, so the Bitnami catalog's move to OCI forced
    single charts over to `v3.Release` (kluster-code#100). Private
    registries need provider ≥4.27 for in-process login; nothing pinned
    here is private.
-   **Hooks are dropped.** Any object annotated `helm.sh/hook` is
    omitted from the rendered output, test hooks unconditionally. The
    pinned set contains exactly one: cert-manager's `startupapicheck`
    post-install Job, which blocks until the webhook answers. It is
    therefore disabled explicitly (`startupapicheck.enabled: false`)
    rather than left to vanish silently, and the wait it did is covered
    by the install order — nothing declares an Issuer or a Certificate
    until cert-manager is up. The provider's `includeHooks` (≥4.33) is
    not a substitute: it only writes hooks into a rendered directory for
    some other tool to apply.
-   **CRDs.** Definitions in a chart's `crds/` directory are installed by
    default and become Pulumi's resources; `skip_crds` opts out. Helm
    never upgrades a CRD it installed that way, which is the reason
    Gateway API is a separate install-order entry rather than something
    a chart brings along. A chart that ships its definitions as ordinary
    templates behind a value instead (cert-manager) is unaffected by
    either switch and needs that value set.
-   **Values may be Outputs.** Because the render happens in the
    provider rather than in the language host, a chart value can be an
    unresolved Output — the limitation that made `v3.Chart` unusable for
    anything wired to another resource. A preview whose values are
    genuinely unknown still cannot enumerate what a template branches
    on.
-   **Transformations** are the generic `transforms` resource option,
    not the chart-specific `transformations` of `v3.Chart`; a transform
    cannot change an object's name or namespace.
-   **Dependency update** exists (`dependency_update`) and is unused:
    every pin here resolves to a packaged archive that already carries
    its dependencies, the VictoriaMetrics stack included — its own
    dependencies are OCI references, and they are inside the archive.
-   **A preview needs a reachable cluster**, because the render is
    server-side dry-run. A preview of this stack cannot succeed before
    `physical` has converged once (`pulumi-kubernetes` issue 3027).
-   **The fallback, recorded.** A chart that genuinely needs its hooks
    to run, or that has to adopt objects it did not create, is installed
    with `helm.sh/v3.Release` instead — one chart at a time, in the
    component that owns it, not by moving the stack.

## 2. Cilium: the load-bearing component

All decided behavior from architecture.md §3, expressed as config:

-   **Datapath**: kube-proxy replacement on; `k8sServiceHost:
    localhost`, `k8sServicePort: 7445` (KubePrism — mandatory, there is
    no kube-proxy to fall back on); dual-stack with IPv4 primary;
    IPv6 masquerade on — pod v6 addresses are internal/unroutable, so
    outbound v6 is SNAT'd to the node's GUA; this *is* qbittorrent's
    outbound-v6 mechanism (architecture.md §3.5); MTU sized for the
    KubeSpan underlay (WireGuard overhead — verify, don't assume).
-   **LB IPAM**: two `CiliumLoadBalancerIPPool`s — `internet` (the
    on-the-wire node addresses: the three primary **private** IPv4s +
    the v6 GUAs + the augmented node's secondary private IP — OCI
    1:1-NATs public v4 to private, so public v4 literals would never
    match (architecture.md §3.2); all from physical outputs) and `lan`
    (`192.168.71.0/24` + the ULA /64, outside every home network and
    the nodes' own VLAN 7 alike). Pool membership via the
    `serviceSelector` label from `conventions.py`. Bootstrap verification: pool-contains-node-IP.
-   **BGP**: Cilium **BGPv2** resources — `CiliumBGPClusterConfig`
    (node-selected to the homelab worker only) +
    `CiliumBGPPeerConfig` + `CiliumBGPAdvertisement`; the v1
    `CiliumBGPPeeringPolicy` is deprecated and **not used** (at the
    ≥1.20 floor it may be removed outright) — peering with the UDM
    (AS 65000) over both families, advertising `lan` VIPs as
    /32 + /128. The UDM side is physical's gw-config provider; the
    session only establishes once both stacks are up — acceptable,
    nothing LAN-facing exists before apps deploy.
    **Session hardening (2026-08-23, architecture.md §4.1)**: an MD5
    session password on both ends — BGPv2's `authSecretRef` on the
    Cilium side; **placement fact on top of §1.1**: the referenced
    Secret must live in the namespace named by
    `--bgp-secrets-namespace` (kube-system by default), so its
    SealedSecret is sealed for that namespace — and a gw-config
    device secret on the UDM side. The UDM's
    FRR config applies an inbound **prefix-list** (`192.168.71.0/24
    le 32` + the ULA /64 `le 128`, deny the rest) plus a
    `maximum-prefix` cap — without the filter, a compromised worker
    VM (or anything claiming its static IP while it's down) could
    advertise arbitrary /32s — the DNS servers' addresses included —
    and MITM the whole LAN. Verified at bootstrap by advertising a
    bogus prefix (physical.md §6).
-   **Gateway API**: enabled; three `Gateway`s — `internet-gw` (Envoy
    replicas across the cloud nodes, `externalTrafficPolicy: Local`,
    Service requesting all three primary IPs via `lbipam.cilium.io/ips`
    + sharing-key), `lan-gw` (pinned to the homelab worker, `lan`
    pool), and `media-gw` (same shape as `lan-gw` on a **second,
    dedicated `lan`-pool VIP** — a `conventions.py` literal, because
    the UDM firewall's IoT→media allow names it,
    physical/gateway.md §4.2). Attaching a route to `media-gw` *is*
    the decision "reachable from the IoT VLAN"; the helper exposes
    it as a parameter, so the choice is visible in the app's diff.
    Apps attach `HTTPRoute`s (§3.6 matrix).
-   **Egress Gateway**: enabled (the dedicated-VIP pattern's outbound
    half, architecture.md §3.2); the `CiliumEgressGatewayPolicy`
    instances themselves belong to the workloads that need them
    (`apps`). Bootstrap verification: EGW under the chosen routing
    mode.
-   **Route-level auth (the Authelia gate)**: the Gateway API
    **ExternalAuth HTTPRoute filter** (GEP-1494; **Cilium ≥1.20 — this
    sets the Cilium version floor**, above the ≥1.16 EGW-tunnel floor)
    pointing at Authelia's Envoy `ext_authz` endpoint. This is how
    apps without native auth (qbittorrent Web UI, golinks, spoolman,
    thread-dashboard, …) get SSO-gated — the legacy traefik
    forward-auth middleware's successor; the route helper exposes it
    as an `auth=True` parameter. Bootstrap verification: confirm the
    filter **fails closed** when Authelia is unreachable (fail-open
    was reported against early builds, cilium#47178). Because every
    `auth=True` app (qbittorrent Web UI included — its "run external
    program" setting makes fail-open an RCE) rides this one mechanism,
    fail-closed is also **verified continuously, not only at
    bootstrap**: a standing **auth canary** — a synthetic
    unauthenticated probe against a protected route, with a vmalert
    rule firing on anything but a 401/302 — so a Cilium upgrade
    regressing to fail-open pages instead of silently exposing every
    gated app, and Cilium bumps merge only with the canary green.
    (Per-app fallback auth layers were considered and rejected: N app
    configs guarding against one mechanism's failure is the wrong
    layer — harden and monitor the mechanism.) Apps with
    native OIDC (immich, grafana, matrix, splitpro) are unaffected by
    this mechanism's availability.
-   **Hubble**: enabled with relay + UI off by default (metrics into
    VictoriaMetrics; the UI is a port-forward away when needed — no
    standing dashboard, per the standing-rent rule).
-   **Network policy stance** (architecture.md §4.1): default-deny is a
    per-namespace app concern; this stack ships only the cluster-wide
    baseline (blocking pod→management-plane except where declared) —
    which **explicitly includes pod egress to `169.254.0.0/16`**: the
    OCI metadata service serves the machine config, and OCI's IMDSv2
    header is static, so this policy is the only thing between a
    compromised pod and the cluster PKI (architecture.md §4.1;
    bootstrap verification in physical.md §6).

## 3. What this stack deliberately does not do

-   No ingress of its own — gateways are wiring; routes/listeners/
    security-rules arrive with apps (the derived-not-enumerated
    principle, physical.md §1).
-   No app namespaces, quotas, or per-app policy.
-   No backup schedules — VolSync `ReplicationSource`s are declared
    beside their PVCs in `apps` via the `backed_pvc` helper and
    retention classes (workloads.md §3); this stack only installs the
    controller, the shared B2 restic secret material, and the
    backup-freshness vmalert rule family.
-   No DNS records (declarative/dns.md).
