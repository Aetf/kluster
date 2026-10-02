# Declarative Design: the `k8s-base` Stack

How the in-cluster foundations are declared — the component set, its
install/dependency order, and the configuration points already decided
elsewhere (architecture.md for the network design, storage.md for
storage, nodes.md §4.4 for why this list is as small as it is). This is
the middle stack of [README.md](README.md) §1: everything cluster-scoped
that speaks the k8s API, consumed by `apps`.

> **Status**: designed 2026-08-22. Declared: Cilium (§2: the datapath,
> the Gateway API definitions and class, the pools and the baseline),
> the sealed-secrets controller, cert-manager's chart,
> local-path-provisioner and reloader. Not yet implemented: the
> Gateways, BGP, cert-manager's issuer and certificates, and the rest
> of §1.

## 0. Scope and rules

-   Inputs: the kubeconfig, a config secret of the stack's own that
    `credentials derived sync --only kubeconfig` copies out of
    `physical`'s state, since a StackReference elides a secret across
    `physical`'s passphrase ([credentials.md](../credentials.md) §3); and,
    by StackReference to `physical` under the names in
    `conventions.PHYSICAL_OUTPUTS`, the addresses the `internet` pool is
    made of — each cloud node's primary private IPv4
    (`node_private_ips`), each node's GUA (`node_guas`), the dedicated
    VIP's secondary private address (`vip1_private`) and the balancer's
    public IPv4 and IPv6 (`cluster_endpoint`, `cluster_endpoint_v6`),
    each read so that anything but an address of its family stops the
    run naming the output (`kluster.lib.stack_addresses`). A stack holding no
    copy of the kubeconfig, or a blank one, stops at the read and names
    that command (`kluster.lib.k8s.kubeconfig_from`). The program builds
    one Kubernetes provider from it and disables the package's default
    provider in its stack file (rfc-007 §3.1).
-   **Names: explicit for shared singletons, outputs for the dynamic.**
    Cross-stack-referenced singletons (StorageClasses, Gateways, pools,
    shared Secret names) get explicit `metadata.name`s with autonaming
    disabled — they are well-known singletons where autonaming only
    hurts (the legacy autonamed-PVC lesson) — and those fixed names live
    in `conventions`. Where autonaming is deliberately kept, the
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
-   **Every namespace this stack creates states its Pod Security
    level** in its `enforce` label. Talos enforces `baseline`
    cluster-wide and exempts `kube-system` alone, and `baseline` refuses
    the host network, the host PID namespace and host paths. So a
    namespace is `restricted`, the level workloads.md §1 gives an
    application, unless its component's pods need the host, and then
    `privileged`, with the reason in the component: the monitoring
    namespace (the node exporter's host network, PID namespace and
    paths), local-path-provisioner's (its helper pods mount the path
    they provision), and NFD's and the GPU plugin's (device and host
    mounts). What installs into `kube-system` — Cilium and the sealing
    controller — stays under Talos' exemption. A chart whose pods need
    no host and still miss `restricted` has its security-context values
    set to meet it where the chart exposes them (reloader's container
    context, empty at its pin, is the instance, and the component sets
    it), and takes `baseline`, with the reason, only where the chart
    does not (rfc-007 §8). Every such namespace is created through
    `kluster.lib.k8s.namespace`, which takes the level as a required
    argument.

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
    component's credentials can be SealedSecrets (§1.1). Installed as
    `sealed-secrets-controller` in `kube-system`, the name and namespace
    `kubeseal` looks for unless told otherwise, so `kubeseal` run by hand
    finds it with no flag; the sealing command names both all the same,
    from `conventions` (rfc-007 §6.1). Migration note: the legacy sealing key is restored
    into the new cluster *before* any legacy SealedSecret manifests are
    ported, or everything gets re-sealed (migration.md).
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
    Alertmanager's one receiver is a Home Assistant webhook of its own,
    an automation apart from the one the operations repository posts to,
    because alertmanager's body is fixed: it reads the tier, the summary
    and the playbook off each alert and pushes under the same title
    convention. The webhook's address is a sealed value
    (`conventions.sealed`'s alert-webhook row, §1.1), so it appears in
    no rendered configuration (rfc-007 §7.3). Alertmanager holds **no
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
    its backing directory is a Talos user volume on every node,
    physical.md §2), declared as resources of this program after the
    release's own manifest rather than installed from a chart, since
    its upstream publishes no chart repository; its namespace is
    `privileged`, because the helper pods it starts mount the path they
    provision (§0),
    **metrics-server**, **reloader**. Monitoring internals
    (kube-state-metrics, the node-exporter DaemonSet) count as part of
    the VictoriaMetrics entry. None has an ordering constraint beyond
    Cilium.

Every chart this list installs is pinned in `Pulumi.yaml`'s `versions:`
block as `versions:chart-<name>`, the Gateway API definitions as
`versions:manifest-gateway-api`, and local-path-provisioner's two images,
the provisioner's and its helper pod's, as `versions:image-<name>`
(framework/pulumi.md §3.2). One copy:
the stack program installs from those pins, and `update_crds` reads the
same file through the same parser to render the CRD bundle
`packages/crds/crds.yaml` from exactly this chart set and regenerate the
SDK the custom resources are declared through, `sdks/crds`, from that
bundle; the legacy chart list retires with kluster-code.
A chart pin carries where the chart is served and its version — with the
digest of its manifest where the chart comes from an OCI registry, which
Helm pulls it by — and what the regeneration needs: whether the chart
renders definitions, the values that make it render them, and the floor
its operator version has to clear with the section that states it. The
floor is checked by `update_crds` against the `appVersion` the chart
declares. Renovate moves the pins (operations.md §1), and a bump of one
the script reads is finished by running `update_crds` on its branch:
the bundle records the pins it was rendered from, and a test holds that
record to the block (framework/pulumi.md §4).

Rendering is **offline**: a pinned Helm 3 binary renders each chart
and the CRDs are filtered out of the result, so the bundle describes
the pinned chart set rather than whatever some cluster happens to have
installed. Two consequences worth naming. Cilium's chart contains no
CRD at all — the agent registers its own at runtime — so its
definitions are read from the checked-in YAML at the release tag the
Cilium chart's pin names, and a chart bump moves both. And the
VictoriaMetrics stack installs only `operator.victoriametrics.com`:
this cluster has no `ServiceMonitor` and no `PodMonitor`, so a scrape
target is declared as the VictoriaMetrics object, never as a
prometheus-operator one. Because the render never contacts a cluster,
it cannot prove the set is *complete* — a chart that creates a
definition at runtime the way Cilium does would simply be missing. The
first live `up` is what proves it.

### 1.1 Secrets placement rules

Two channels, chosen by who consumes the secret:

-   **Consumed in-cluster as a k8s Secret → SealedSecret, first
    choice** — the kluster-code model carries over unchanged (including
    the `template.data` pattern: plaintext config stays reviewable in
    git, only the sensitive fields are sealed). Examples: cert-manager's
    DNS-01 token, app credentials, VolSync restic passwords, CNPG user
    secrets.
    -   **A sealed value is a plain value in the configuration of the
        stack that declares it**, under the one structured key
        `sealedSecrets`, keyed by the value's name and then by its data
        key. The ciphertext opens with the cluster's sealing key alone,
        so it is committed in the clear beside the stack's config
        secrets; the stack program reads it and hands each component
        the ciphertext of the values it declares, and the component
        declares the SealedSecret through `kluster.lib.k8s.sealed_secret`.
    -   **The sealed values are a census**, `conventions.sealed`,
        because two programs agree on each one: the `credentials`
        command seals and writes it, and a stack declares it. Each row
        names the Secret the value becomes, its namespace, the keys of
        its data, the scope it is sealed at and the stack that declares
        it, and the configuration path follows from the row. A value
        `credentials` seals is sealed strict — name and namespace bound
        into the ciphertext, `kubeseal`'s own default — since nothing
        about it will be renamed ([credentials.md](../credentials.md)
        §1, rule 6, and §3).
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
    ACME issuance (dns.md §4) delivered as a device secret.

### 1.2 Installing a chart: `helm.v4.Chart`

The API every component installs through, wrapped as
`kluster.lib.k8s.helm_chart`:

-   **What it is.** `helm.v4.Chart` renders the chart in the provider
    and hands each rendered object to Pulumi as its own resource. It
    does not create a Helm release, which is what `helm.sh/v3.Release`
    does instead. The trade is per-object diffs, drift remediation and
    Pulumi transforms and policies, against losing `helm list`
    visibility, release history and `helm rollback`, and the ability to
    adopt an existing release. This cluster is built from empty and
    every object in it is Pulumi's, so the losing side is empty too.
-   **OCI registries.** A chart reference may be a full `oci://` URL in
    `chart` itself, with no repository options beside it; `helm_chart`
    writes a pin's digest into that reference, and Helm refuses the
    pull when the version's tag resolves to another manifest. This is the
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

-   **Datapath**: the installation Cilium's and Talos' guides both
    document for Talos (rfc-007 §4.1) — Kubernetes IPAM, since Talos
    assigns every node its pod ranges; the agent's capabilities without
    `SYS_MODULE`, which Talos lets no workload use; Talos' own
    control-group mount; and kube-proxy replacement on, reaching the API
    server through KubePrism (`k8sServiceHost: localhost`,
    `k8sServicePort: 7445` — mandatory, there is no kube-proxy to fall
    back on). What the design adds: dual-stack with IPv4 primary; tunnel
    routing over `vxlan`, whose packets are addressed node to node and so
    ride KubeSpan; the MTU of KubeSpan's link, `conventions.KUBESPAN_MTU`,
    stated rather than detected, because the agent takes the tunnel's
    overhead off the MTU it is given and KubeSpan's link is selected by a
    firewall mark, which detection would miss; and policies that leave
    default-deny off, which the baseline below is. **BPF masquerading
    and the legacy host-routing switch stay unset**: BPF masquerading
    brings BPF host routing, which bypasses `netfilter` in the host's
    namespace, where the node firewall and KubeSpan's steering both
    live, and both upstreams answer that with the legacy switch; unset,
    masquerading is the iptables implementation and host routing takes
    the host's stack. Both families masquerade — pod v6 addresses are
    internal/unroutable, so outbound v6 is SNAT'd to the node's GUA; this
    *is* qbittorrent's outbound-v6 mechanism (architecture.md §3.5).
    Metrics: the agent's, the operator's and Envoy's endpoints, and
    Hubble's flow metrics.
-   **No `NodePort`** (Aetf/kluster-ops#397): whatever the datapath
    answers on a node's own address is internet-facing on a cloud
    node, because the subnet admits everything and the datapath
    answers a raw Service's frontend in BPF before the Talos ingress
    firewall sees the packet (physical.md §1–2). That set must be the
    declared `internet`-pool
    frontends and nothing else. hostPort and `externalIPs` are already
    forbidden (workloads.md §1), so the only frontends outside it are
    node ports, and **no Service allocates one**. Kubernetes allocates
    them to every LoadBalancer Service unless the Service sets
    `allocateLoadBalancerNodePorts: false`, and the kube-proxy
    replacement serves them on every node by default; a `lan`-pool raw
    TCP/UDP Service, whose traffic policy is `Cluster` (architecture.md
    §3.1), would then answer at a cloud node's public address. The
    Gateways' Services opt out through their `CiliumGatewayClassConfig`
    (below), `public_port` sets the field on its Service
    (workloads.md §1), and `type: NodePort` is on workloads.md §1's
    list of what a component may not do. Bootstrap verification: no
    Service carries a `nodePort` (physical.md §6). A `Local` Service
    still carries a `healthCheckNodePort`, which Kubernetes allocates
    whatever `allocateLoadBalancerNodePorts` says. Cilium answers it
    from a listener in the host network namespace, which the Talos
    firewall filters, so **`enable-health-check-loadbalancer-ip` stays
    off**: turned on, it adds a BPF frontend for that port on the
    Service's load-balancer address, which for the `internet` pool is a
    node's own address, ahead of the firewall. The chart's values state
    it.
-   **LB IPAM**: two `CiliumLoadBalancerIPPool`s — `internet` (the
    on-the-wire node addresses: the three primary **private** IPv4s +
    the v6 GUAs + the dedicated-VIP node's secondary private IP — OCI
    1:1-NATs a node's public v4 to its private one, so a node's public
    v4 literal would never match (architecture.md §3.2) — and the
    balancer's public IPv4 and IPv6, a class of its own that no arriving
    traffic carries and that answers, on the caller's own node, a
    connection that starts in the cluster; all from physical outputs,
    one single-address block each) and `lan` (`192.168.71.0/24` + the
    ULA /64, outside every home network and the nodes' own VLAN 7
    alike). Pool membership via the `serviceSelector` label from
    `conventions`. **Every Service on the `internet` pool's node
    addresses takes `Cluster`** (architecture.md §3.1), and **a Service
    whose census row the balancer fronts asks for the balancer's two
    addresses beside the node addresses**, so a pod on a cloud node
    reaching one of the cluster's public names is answered without the
    balancer, which a node it forwards to cannot reach (architecture.md
    §3.2). Bootstrap verification: pool-contains-node-IP.
-   **BGP**: Cilium **BGPv2** resources — `CiliumBGPClusterConfig`
    (node-selected to the homelab worker only) +
    `CiliumBGPPeerConfig` + `CiliumBGPAdvertisement`; the v1
    `CiliumBGPPeeringPolicy` is deprecated and **not used** (at the
    ≥1.20 floor it may be removed outright) — peering with the UDM
    (AS 65000) over both families, advertising `lan` VIPs as
    /32 + /128. The UDM side is physical's device-files provider; the
    session only establishes once both stacks are up — acceptable,
    nothing LAN-facing exists before apps deploy.
    **Session hardening (2026-08-23, architecture.md §4.1)**: an MD5
    session password on both ends — BGPv2's `authSecretRef` on the
    Cilium side; **placement fact on top of §1.1**: the referenced
    Secret must live in the namespace named by
    `--bgp-secrets-namespace` (kube-system by default), so its
    SealedSecret is sealed for that namespace — and a device
    secret on the UDM side. The UDM's
    FRR config applies an inbound **prefix-list** (`192.168.71.0/24
    le 32` + the ULA /64 `le 128`, deny the rest) plus a
    `maximum-prefix` cap — without the filter, a compromised worker
    VM (or anything claiming its static IP while it's down) could
    advertise arbitrary /32s — the DNS servers' addresses included —
    and MITM the whole LAN. Verified at bootstrap by advertising a
    bogus prefix (physical.md §6).
-   **Gateway API**: enabled; Envoy is the release's per-node
    DaemonSet, on every node, and Gateway traffic goes to the Envoy on
    the node it arrives at before any backend is chosen, so where a
    Gateway answers is decided by where its address is routed, and the
    client's address reaches Envoy under either traffic policy
    (rfc-007 §5.1). Three `Gateway`s — `internet-gw` (its Service
    requesting the `internet` pool's node members and the balancer's two
    addresses via `lbipam.cilium.io/ips` + sharing-key), `lan-gw` (the
    `lan` pool's default VIP, which the homelab worker announces), and
    `media-gw` (same shape as `lan-gw` on a **second,
    dedicated `lan`-pool VIP** — a `conventions` literal, because
    the UDM firewall's IoT→media allow names it,
    physical/gateway.md §4.2). Attaching a route to `media-gw` *is*
    the decision "reachable from the IoT VLAN", and the route census
    row records it as `Exposure.IOT` (conventions/routes.py), so the
    choice is on the row a reviewer reads rather than an argument at a
    call site. Apps attach `HTTPRoute`s (architecture.md §3.6 matrix).
    The Gateways share one `GatewayClass`, `cilium`, which the stack
    declares in place of the chart's own so that it can carry a
    configuration: its `parametersRef` names a `CiliumGatewayClassConfig`
    (`cilium.io/v2alpha1`, in `kube-system`) setting
    `spec.service.externalTrafficPolicy: Cluster`, which a
    configuration that sets any field imposes anyway and so is stated,
    `allocateLoadBalancerNodePorts: false`, so the Gateways' Services
    allocate no `NodePort`, and both IP families required. The
    Gateways' secret sync, which hands Envoy the certificates, stays on
    and is stated.
-   **Egress Gateway**: not enabled. It refuses to start without BPF
    masquerading, which on this platform brings the legacy host-routing
    switch with it (Datapath, above). Only hath would use it, for the
    dedicated-VIP pattern's outbound half (architecture.md §3.2), and
    hath's migration wave decides between it and an address of the
    node's own (rfc-007 §15.3).
-   **Route-level auth (the Authelia gate)**: the Gateway API
    **ExternalAuth HTTPRoute filter** (GEP-1494; **Cilium ≥1.20 — this
    sets the Cilium version floor**)
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
-   **Hubble**: enabled with relay + UI off (its flow metrics — drops,
    TCP, flows, ICMP — into VictoriaMetrics; the UI is a port-forward
    away when needed — no standing dashboard, per the standing-rent
    rule).
-   **Network policy stance** (architecture.md §4.1): default-deny is a
    per-namespace app concern; this stack ships only the cluster-wide
    baseline, a `CiliumClusterwideNetworkPolicy` selecting every pod that
    **denies egress to `169.254.0.0/16` except `169.254.116.108/32`**:
    the OCI metadata service serves the machine config, and OCI's IMDSv2
    header is static, so this policy is the only thing between a
    compromised pod and the cluster PKI (architecture.md §4.1;
    bootstrap verification in physical.md §6); the one address excepted
    is where Talos' host DNS answers pods, which the cluster DNS forwards
    to. A deny rule alone switches every pod it selects to default-deny
    egress, so the baseline sets `enableDefaultDeny` false in both
    directions and leaves default-deny to the per-namespace policies
    (rfc-007 §4.5). The baseline's wider intent, blocking pods from the
    management plane except where declared, is **not built**: rfc-007
    §4.5 scopes M2's baseline to the metadata range. A pod's call to
    another node's management port leaves its node to a node address,
    rides KubeSpan and enters on `kubespan`, which the ingress chain
    accepts ahead of every rule, so each service's own authentication is
    all that stands in its way. Building that block in a later slice or
    retiring the intent is the operator's decision,
    [Aetf/kluster-ops#499](https://github.com/Aetf/kluster-ops/issues/499).

## 3. What this stack deliberately does not do

-   No ingress of its own — gateways are wiring; routes arrive with
    apps, and the balancer's listeners for public ports are `physical`'s
    (physical.md §1).
-   No app namespaces, quotas, or per-app policy.
-   No backup schedules — VolSync `ReplicationSource`s are declared
    beside their PVCs in `apps` via the `backed_pvc` helper and
    retention classes (workloads.md §3); this stack only installs the
    controller, the shared B2 restic secret material, and the
    backup-freshness vmalert rule family.
-   No DNS records (declarative/dns.md).
