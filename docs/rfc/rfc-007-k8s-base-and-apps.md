# RFC 007: The `k8s-base` and `apps` Stacks

*   **Status:** Accepted, 2026-10-01. The operator approved the design
    after two review rounds whose answers are written into the text: the
    chart pins and sealed values in stack configuration, local-path
    declared natively, the recommended Cilium installation on Talos, the
    Gateway's certificate renewal over SDS, and §15.1 ruled (a). §15.3
    records the two decisions left to later changes.
*   **Created:** 2026-09-28
*   **Updated:** 2026-10-02 — §3.4, as slice 4 built it: a chart pin
    carries its manifest's digest wherever the publisher's OCI registry
    has the pinned version; two managers read the chart pins, through the
    Helm data source for an HTTP repository and the docker one for an OCI
    registry; the record covers every chart pin the script reads, one
    carrying only a floor included; and the manifest fetch lives in
    `kluster.lib.release_assets`, which imports no bindings, rather than
    in `kluster.lib.k8s`.
*   **Updated:** 2026-10-02 — §2, §3.1, §3.3 and §13: the kubeconfig
    reaches both programs as a config secret of their own, copied out of
    `physical`'s state by `credentials derived sync --only kubeconfig`,
    rather than across a StackReference, which elides it once `physical`
    is under a passphrase of its own (rfc-005 §5.1); each program refuses
    an absent or unusable copy at the read, naming that command. Slice
    3's text keeps the StackReference read it built.
*   **Authority:** AGENTS.md,
    [framework/dispatch.md](../framework/dispatch.md),
    [framework/rfc.md](../framework/rfc.md) and the style rules
    (`docs/style/`) are what this document obeys. A rule proposed here
    that they do not state is marked **new rule** where it is stated.
*   **Companion:** [rfc-002](rfc-002-src-layout-and-the-gateway.md), for
    the layering, the component per unit of resources and the explicit
    provider (rfc-002 §8); [rfc-003](rfc-003-dns-and-github-stacks.md),
    for the route census, the exposure model, and the seam between the
    stack that declares a route and the stack that writes its rewrite
    (rfc-003 §6). Their precedents are cited at each use rather than
    re-argued.
*   **In scope:**
    1.  what the two stack programs read, the order their first applies
        run in, and what makes a preview of each green;
    2.  Cilium's datapath and the half of it the machine configuration
        carries, the host ports the node firewall opens for it, the two
        address pools, the baseline policy and the BGP session;
    3.  the front door: the Gateways' Service and its traffic policy,
        their listeners and certificates, and the balancer's listeners
        for the public port census;
    4.  sealed-secrets: the controller, where a sealed value lives and
        what writes it;
    5.  monitoring, and the alert legs that run inside the cluster;
    6.  the rest of the closed component list, as install steps, and
        the Pod Security level of each namespace the stack creates;
    7.  the first `apps` component, where the route helper lives, and
        the zone set of a route that publishes no public record;
    8.  where the design documents contradict each other or the pinned
        upstream releases, each with both sites (§10);
    9.  the slices.
*   **Out of scope:**
    *   **The cloud subnet's filter and the rule that no Service
        allocates a `NodePort`.** Decided and built on 2026-09-28
        ([Aetf/kluster#405](https://github.com/Aetf/kluster/pull/405)):
        the subnet admits everything, the Talos ingress firewall is the
        only filter on the host network namespace, and the kubelet is
        opened to the cluster's own ranges. It is stated in
        [declarative/physical.md](../declarative/physical.md) §1 and §2,
        [declarative/cluster-infra.md](../declarative/cluster-infra.md)
        §2 and [declarative/workloads.md](../declarative/workloads.md) §1.
        This document builds on it.
    *   **Whether a branch that has not merged runs with stack
        credentials**, which decides whether pull requests preview at
        all. That is the pull-request partition's own design, landing in
        [framework/ci.md](../framework/ci.md) §3. §3.3 says what green
        means under either answer.
    *   **The operations repository's poller and dispatch handler.**
        [cluster/architecture.md](../cluster/architecture.md) §4.3
        designs them, and they are that repository's workflows.
    *   **Home Assistant's automations.** They are the home automation
        system's own configuration. §7.3 states the contract they are
        built against, which lands in
        [operations.md](../operations.md) §4.
    *   **The migration waves, and every application but the first.**
        [cluster/migration.md](../cluster/migration.md) §2 orders them,
        and the milestone that moves them opens with a design of its own.
        §7.4 and §15.3 name what this document leaves to it.
    *   **The threat model**, a document of its own (rfc-005, proposed in
        [Aetf/kluster#399](https://github.com/Aetf/kluster/pull/399)).
    *   **The site gateway's own rewrite-only names**, the console and
        the resolver interfaces. They answer the site gateway's proxy,
        not a cluster address ([declarative/dns.md](../declarative/dns.md)
        §4), and their producer is the `dns` stack's.
    *   **Which generator produces the CRD bindings.** Nothing here
        depends on it ([framework/pulumi.md](../framework/pulumi.md) §4).

--------------------------------------------------------------------------------

## 1. Context and problem statement

The `physical` stack is declared in full and is about to be applied for
the first time. The two stacks above it are not written: each
entrypoint raises `NotImplementedError`, no stack of either name exists
in the state backend, and every pull request that touches code runs a
`k8s-base` and an `apps` preview that fail at `no stack named`
([framework/ci.md](../framework/ci.md) §5). The milestone this document
opens — M2 of the roadmap the operations repository keeps, after M1's
first `physical` bring-up — ends when a workload answers through the
balancer in front of the cloud nodes and an alert reaches the operator's
phone.

Most of that is decided. [cluster/architecture.md](../cluster/architecture.md)
§2 and §3 fix the network design, and
[declarative/cluster-infra.md](../declarative/cluster-infra.md) fixes the
closed component list, its install order and Cilium's configuration.
[declarative/workloads.md](../declarative/workloads.md) fixes the
application contract. What is left is of three kinds:

*   **Decisions the documents leave open**, because they wait on the
    stacks: how either program reaches the cluster and when its preview
    can pass, which host ports the node firewall opens for Cilium, who
    declares the balancer's listeners for workload ports, where a sealed
    value is kept, which alerts ship first, and which zones a route with
    no public record is published in.
*   **Decisions whose premise the pinned upstream releases contradict.**
    The Gateways are designed as Envoy replicas pinned to nodes and run
    under the `Local` traffic policy, while Cilium runs one Envoy per
    node, hands Gateway traffic to it through the host's stack before a
    backend is chosen, and will not share an address between Services
    whose policies differ. Every Service port is designed to be answered
    ahead of the node firewall, which is not true of a Gateway's.
    The baseline policy denies pods all of `169.254.0.0/16`, which holds
    the address the machine configuration hands pods for DNS. The
    control-plane configuration leaves kube-proxy running under a
    datapath designed to replace it.
*   **Places where the documents disagree with each other**, collected
    with the two kinds above in §10.

The stacks are cheap to shape now: nothing is applied, and the physical
changes this design needs can still ride the first `physical` apply
rather than a gated one of their own (§15.1).

--------------------------------------------------------------------------------

## 2. What is inherited, and what is decided here

| Precedent | Applied here |
| --- | --- |
| The closed component list and its install order (item 1 to item 8 of [cluster-infra.md](../declarative/cluster-infra.md) §1) | One component area per entry, wired by the stack program in that order (§3.1, §8) |
| Secrets placement: a Secret the cluster consumes is a SealedSecret (cluster-infra.md §1.1) | Where the ciphertext lives and what writes it (§6.2) |
| Installing a chart through `helm.v4.Chart` (cluster-infra.md §1.2), and a preview of it needing a reachable cluster | What a green preview needs (§3.3) |
| The two pools, the three Gateways, BGP to the gateway, the Egress Gateway and ExternalAuth ([architecture.md](../cluster/architecture.md) §3, cluster-infra.md §2) | Cilium's values and objects (§4, §5); the Egress Gateway waits for hath's wave (§4.1) |
| No Service allocates a `NodePort`, and the Talos ingress firewall is the only filter (built; out of scope above) | The Gateways' class configuration carries it (§5.1); the host ports Cilium adds are openings in that firewall (§4.3) |
| Every provider explicit, its credential read at the line that builds it ([style/pulumi.md](../style/pulumi.md), "Layering"; rfc-002 §8) | The Kubernetes provider of both stacks, and the Cloudflare provider of `apps` (§3.1) |
| What crosses a stack boundary: machine facts by StackReference, decisions by `conventions` ([declarative/README.md](../declarative/README.md) §2) | The pool addresses and the zone identifiers; the kubeconfig, a secret no StackReference carries across `physical`'s passphrase, by a copy into each stack's configuration (§3.1) |
| A census read by more than one program is a convention (style/pulumi.md, "Data") | The public port census gains readers (§5.3); the sealed values become a census (§6.2) |
| Every version pin a stack program reads lives in `Pulumi.yaml`'s `versions:` block, and a pin a script reads too in a `kluster.lib` module ([framework/pulumi.md](../framework/pulumi.md) §3.2) | The chart and manifest pins join the block as structured values, and the second half is reversed: the script reads the block from the file through the program's parser (§3.4) |
| Code a component and a script both run lives in `kluster.lib.<area>` (style/pulumi.md, "Layering") | The manifest fetch, run by the program and `update_crds`, in `kluster.lib.release_assets`, which imports no bindings (§3.4) |
| The installation Cilium's and Talos' guides document for Talos | Taken as written, and every addition is a setting the design needs (§4.1) |
| The route census, the exposure model and the one `route(row)` helper (rfc-003 §6, [dns.md](../declarative/dns.md) §5) | The helper's home, and the zone set of a LAN-side row (§9) |
| The alert channel, its tiers and the playbook rule (architecture.md §4.3, [operations.md](../operations.md) §4 and §5) | The in-cluster rules and the push leg (§7) |
| Stop-copy-start and the sealing key's restore ([migration.md](../cluster/migration.md) §0 and §1) | The restore moves to the first ported manifest (§6.3) |

--------------------------------------------------------------------------------

## 3. The two programs

### 3.1 What each program reads

Both are wiring, as every stack program is (style/pulumi.md,
"Layering"): each reads its configuration and its StackReferences,
builds its providers, and calls one component per unit.

*   **`k8s-base`** reads, from its own configuration, the
    kubeconfig, and, from the `physical` stack's outputs, the addresses
    the `internet` pool is made of (§4.4). It builds one Kubernetes
    provider from the kubeconfig and disables the package's default
    provider in its stack file. It calls one component per entry of the
    closed list, in cluster-infra.md §1's order, and the order is also
    the parent and `depends_on` chain that lets one `up` converge from an
    empty cluster.
*   **`apps`** reads the kubeconfig the same way, and `dns`'s zone
    identifiers for the public records it declares beside each
    application ([dns.md](../declarative/dns.md) §1). It builds the
    Kubernetes provider and the Cloudflare provider, the latter from the
    zones token in its own configuration, which the credential register
    already names `apps` a consumer of
    ([credentials.md](../credentials.md) §3); the slot map gains the
    `apps` target when the stack exists (slice 3).

**The kubeconfig is a config secret of each stack's own**, under the key
`kubeconfig`, because a StackReference cannot carry it: `physical` is
encrypted under a passphrase of its own (rfc-005 §5.1), and a
StackReference elides every secret output the reading stack cannot
decrypt, reading the kubeconfig back as `{}`. `credentials derived sync
--only kubeconfig` copies it out of `physical`'s state into both stacks'
configuration, reading under `physical`'s passphrase and writing under
the stack passphrase, and is run again whenever `physical` changes it
([credentials.md](../credentials.md) §3).

**Both read it so that anything but a kubeconfig stops the run at that
line** (`kluster.lib.k8s.kubeconfig_from`): a stack holding no copy says
which key is missing and names the command that fills it, and a copy
that is blank or Pulumi's unknown sentinel says what it found. The
alternative, an unknown or unusable kubeconfig handed to the provider,
is worse on both counts: `pulumi-kubernetes` marks the
cluster unreachable and previews plain resources by echoing their inputs
([`provider.go` L861–867](https://github.com/pulumi/pulumi-kubernetes/blob/v4.34.1/provider/pkg/provider/provider.go#L861-L867)),
while `helm.v4.Chart` refuses outright with "configured Kubernetes
cluster is unreachable"
([`provider_construct.go` L72–74](https://github.com/pulumi/pulumi-kubernetes/blob/v4.34.1/provider/pkg/provider/provider_construct.go#L72-L74)).
So one stack would preview green over nothing and the other red for a
reason nobody can read off the log.

The StackReference sentence in style/pulumi.md, which names the `dns`
stack's anchors as the one use today, says why a secret such as the
kubeconfig travels as a copy instead.

### 3.2 The stacks exist, and the order the first applies run in

**The stacks are created by the operator**, with `pulumi stack init` from
the checkout that holds the state backend's bundle and the stack
passphrase, and each `Pulumi.<stack>.yaml` is committed with its
`encryptionsalt` and its disabled default providers. That is the one step
of slice 3 no agent can take.

**The first applies are the deploy chain's, not a ceremony's.** The
chain applies `k8s-base` after `physical` and `apps` after `k8s-base` on
every push to `main` ([framework/ci.md](../framework/ci.md) §3), and it
cannot reach them before the end of the first `physical` bring-up,
because `plan-physical` fails at the overlay join until the
continuous-integration identity is delivered (ci.md §5). So every slice
that merges before that delivery is applied together by the first push
after it, and every later slice by its own merge. The install order of
cluster-infra.md §1 is what makes the first of those converge in one
`up`.

That holds for everything but a consumer of a sealed value. A sealed
value can be produced only once the controller that opens it is running
(§6.2), so a slice that declares one merges after the controller has
been applied and after the operator has sealed its value — slices 9,
10 and 11 of §14.

### 3.3 What a green preview needs

A preview of either stack is green when each of these holds:

1.  the stack exists in the backend and its stack file is committed
    (§3.2);
2.  its entrypoint no longer raises;
3.  `physical` has been applied, so the kubeconfig output exists, and
    its copy is in the stack's configuration (§3.1);
4.  the cluster endpoint answers from wherever the preview runs, which
    for CI is the balancer's public 6443
    ([framework/ci.md](../framework/ci.md) §2); `helm.v4.Chart` renders
    with the server's version and API list and refuses without them
    ([`chart.go` L368–396](https://github.com/pulumi/pulumi-kubernetes/blob/v4.34.1/provider/pkg/provider/helm/v4/chart.go#L368-L396)).

What a green `apps` preview does **not** prove is that the resources
`k8s-base` defines exist: a custom resource whose definition is absent
previews by echoing its inputs, with no server-side dry run
([`provider.go` L1318–1332](https://github.com/pulumi/pulumi-kubernetes/blob/v4.34.1/provider/pkg/provider/provider.go#L1318-L1332)).
The deploy chain's order is what makes the apply correct, which is the
accepted reading of parallel previews already (ci.md §3).

Whether pull requests preview at all is not this document's to decide
(out of scope, above). If they do, the four conditions are what turns
`preview (k8s-base)` and `preview (apps)` green, and slice 3 replaces
ci.md §5's `no stack named` paragraph with the red the third condition
leaves until the first `physical` apply and the copy (§3.1). If they do
not, the same four are what turn the chain's `up-k8s-base` and
`up-apps` green, and the weekly drift run is what previews them.

### 3.4 The chart set, in the `versions:` block

**Every chart `k8s-base` installs is a structured pin in `Pulumi.yaml`'s
`versions:` block**, `versions:chart-<name>`, one value per chart. It
holds the chart's repository and version — with the digest of the
chart's manifest wherever the publisher's OCI registry has that version,
which Helm pulls the chart by — and what `update_crds` needs to render
and check it: whether it carries definitions, the values that
make it render them, and the floor its operator version has to clear,
with the document that states the floor. A structured value is written
under `value:`, the one form Pulumi's project schema accepts for an
object; written directly under the key, the object is refused as an
invalid type declaration. The Gateway API definitions are a pin of a
new kind, `versions:manifest-<name>`: the release, the asset and the
asset's sha256. What only the script runs stays in `update_crds/pins.py`
— the Helm binary and the CRD generator with their digests — and so do
the paths of the Cilium source tree the script renders definitions from,
whose ref is the Cilium chart's version, read from that chart's pin
rather than held equal to a second copy of it.

**This reverses the rule of framework/pulumi.md §3.2 that a pin a stack
program and a script both read lives in a `kluster.lib` module.** The
rule rests on `lib/versions.py` reading the block through the Pulumi
SDK, which answers only inside a program. A script needs no SDK to read
the block: it is YAML in a file the script can open, and it is the file
Pulumi itself reads. **The rule becomes: a pin a stack program and a
script both read lives in the `versions:` block, and the script reads
`Pulumi.yaml` itself, through the parser the program's accessor uses.**
**New rule**, replacing the old one's text in framework/pulumi.md §3.2.
The state-backend appliance's pins are the case the old text was
written for, and whether they move is that appliance's own change
(§15.3).

**One access wrapper, two sources.** `lib/versions.py` parses a pin out
of a mapping and refuses a missing or malformed one by naming its key;
a program hands it the mapping `pulumi.Config('versions')` reads, and
`update_crds` the `config:` block of `Pulumi.yaml`. The script reads the
file rather than asking `pulumi`. `pulumi config` answers only for a
selected stack, which means reaching the state backend and holding the
stack passphrase; reading a pin needs neither, and the test that holds
the script's record runs in `checks`, which has neither. The pinned
CLI, given no backend, logs in to Pulumi Cloud instead. The parse
being the accessor's is what keeps the script and the program from
disagreeing on a pin's shape. A pin the
script reads is never overridden in a stack's own file, which the script
does not read; a test holds that no stack file carries one.

**Renovate reads the block**, through custom managers: a chart's
repository and version from one match, through the Helm data source for
an HTTP repository, and with its digest through the docker data source
for an OCI registry, which moves the version and the digest together; a
manifest's release and digest from one match, through
`github-release-attachments`, the data source the CRD generator's pair
already uses, which takes an asset's digest from a checksum file the
release publishes or, where there is none, by hashing the asset
([`github-release-attachments/index.ts`](https://github.com/renovatebot/renovate/blob/main/lib/modules/datasource/github-release-attachments/index.ts)).
A bump is still finished by running `update_crds`, which renovate cannot
do, and what makes it safe to open is a record: **`update_crds` writes
into `packages/crds` the pins it read — the ones it renders from, and a
chart pin carrying a floor it checks — and a test holds that record to
the block.** A bump of a pin the record holds is then red in `checks`
until someone runs `update_crds` on the branch — how a bump of the CRD
generator already finishes — while a bump of one the script reads
nothing from needs nothing more. The floor is checked by `update_crds`
against the operator version the chart itself declares, so the block
carries no hand-kept operator version for a bump to leave stale. **New
rule** for the record, landing in framework/pulumi.md §4.

**The one manifest left is the Gateway API definitions**, which the
program applies and `update_crds` renders from. One function in
`kluster.lib.release_assets` fetches it and refuses an asset whose
digest differs, run by both: code a component and a script both run,
which style/pulumi.md ("Layering") puts in `kluster.lib`. The module
imports no generated bindings, so `update_crds`, which regenerates them,
starts when they do not import. local-path-provisioner is no manifest: it is declared as resources of this program, its image
an ordinary image pin (§8).

--------------------------------------------------------------------------------

## 4. Cilium

### 4.1 The recommended installation, and what the design adds

**The installation is the one both projects document for Talos, and
every other setting is a setting the design needs.** Cilium's guide for
Talos and Talos' guide for Cilium prescribe the same values
([Cilium: Talos L62–80](https://github.com/cilium/cilium/blob/v1.20.1/Documentation/installation/k8s-install-talos-linux.rst#L62-L80),
[Talos: Deploying Cilium](https://docs.siderolabs.com/kubernetes-guides/cni/deploying-cilium)), and each answers a property of Talos
rather than working around one:

*   `ipam.mode: kubernetes`, because Talos assigns every node its pod
    ranges ([Cilium: Talos L18–27](https://github.com/cilium/cilium/blob/v1.20.1/Documentation/installation/k8s-install-talos-linux.rst#L18-L27));
*   the agent's capability list without `SYS_MODULE`, because Talos does
    not let a workload load kernel modules
    ([Cilium: Talos L9–13](https://github.com/cilium/cilium/blob/v1.20.1/Documentation/installation/k8s-install-talos-linux.rst#L9-L13));
*   `cgroup.autoMount.enabled: false` with `cgroup.hostRoot:
    /sys/fs/cgroup`, because Talos already mounts the control-group
    hierarchy;
*   `kubeProxyReplacement: true` with `k8sServiceHost: localhost` and
    `k8sServicePort: 7445`, because without kube-proxy the agent reaches
    the API server through KubePrism.

The Talos half of the same installation — no CNI of Talos' own, and no
kube-proxy — is §4.2. What the design adds to it, each with its reason:

*   **Tunnel routing, `vxlan`, over KubeSpan.** The machine configuration
    leaves KubeSpan's `advertiseKubernetesNetworks` off, and with it off
    KubeSpan carries node-to-node traffic only while "pod-to-pod traffic
    is routed and encapsulated by the CNI plugin"
    ([Talos KubeSpan](https://docs.siderolabs.com/talos/v1.13/networking/kubespan)).
    Cilium's defaults are exactly that shape: `routingMode` tunnel,
    `tunnelProtocol` `vxlan` on UDP 8472
    ([`values.yaml` L3176–3199](https://github.com/cilium/cilium/blob/v1.20.1/install/kubernetes/cilium/values.yaml#L3176-L3199)).
    The encapsulated packets are addressed node to node, and Talos sends
    everything addressed to a peer's node addresses into the WireGuard
    link, forcing it even for a peer that is down
    ([`manager.go` L303–320](https://github.com/siderolabs/talos/blob/v1.13.9/internal/app/machined/pkg/controllers/kubespan/manager.go#L303-L320)).
    Talos' KubeSpan page says that without native routing "Cilium's
    default configuration works with KubeSpan out of the box", and
    native routing is rejected here: Talos supports it with KubeSpan only
    by advertising the pod networks over the mesh, which buys nothing at
    three cloud nodes and one worker.
*   **The MTU is KubeSpan's, stated rather than detected.** The chart's
    `MTU` is the underlying network's — the agent's flag is "Overwrite
    auto-detected MTU of underlying network"
    ([`cell.go` L82](https://github.com/cilium/cilium/blob/v1.20.1/pkg/mtu/cell.go#L82)) — and Cilium subtracts the
    tunnel's overhead from it itself
    ([`mtu.go` L120–165](https://github.com/cilium/cilium/blob/v1.20.1/pkg/mtu/mtu.go#L120-L165)). Detected, it
    would be the node's own interface's, while the link the tunnel
    crosses is KubeSpan's, which Talos selects by a firewall mark rather
    than by a route
    ([Talos: KubeSpan](https://docs.siderolabs.com/talos/v1.13/learn-more/kubespan)).
    So `MTU` is KubeSpan's link MTU, and that becomes a convention both
    programs read, `conventions.KUBESPAN_MTU`, set to Talos' default of
    1420
    ([`constants.go` L1054–1061](https://github.com/siderolabs/talos/blob/v1.13.9/pkg/machinery/constants/constants.go#L1054-L1061))
    and stated in the machine configuration's `KubeSpanConfig` rather
    than inherited, so that a changed Talos default cannot leave Cilium
    sized for a link that no longer exists (§4.2). The bootstrap check
    of MTU over KubeSpan stays, now as a check of a derivation.
*   **Policies that leave default-deny off are enabled, and said so.**
    The baseline of §4.5 is such a policy. With
    `enableNonDefaultDenyPolicies` off, the agent forces default-deny on
    in both directions for every rule
    ([`rule_validation.go` L47–64](https://github.com/cilium/cilium/blob/v1.20.1/pkg/policy/api/rule_validation.go#L47-L64)),
    and the baseline would deny every pod all egress. The chart turns it
    on ([`values.yaml` L4348–4349](https://github.com/cilium/cilium/blob/v1.20.1/install/kubernetes/cilium/values.yaml#L4348-L4349));
    the values state it anyway, as §4.2 states Talos' host DNS.
*   **The features the design is built on**: both address families, the
    Gateway API, the address pools (§4.4) and BGP (§4.6), each switched
    on as cluster-infra.md §2 describes; and the Gateways' secret sync
    (§5.2), which is on by default and stated.
*   **Envoy is the per-node DaemonSet**, the release's default for a new
    installation
    ([`values.yaml` L2721–2741](https://github.com/cilium/cilium/blob/v1.20.1/install/kubernetes/cilium/values.yaml#L2721-L2741)),
    on every node, the worker included. §5.1 is what that changes about
    the Gateways.
*   **Metrics**: the agent's Prometheus endpoint and Hubble's metrics
    are switched on, both off by default, beside the operator's and
    Envoy's, which are on
    ([`values.yaml` L1477–1501, L2654–2657](https://github.com/cilium/cilium/blob/v1.20.1/install/kubernetes/cilium/values.yaml#L1477-L1501)).
    Hubble Relay and its interface stay off (cluster-infra.md §2).

**What it does not add: BPF masquerading, a legacy-routing switch, the
Egress Gateway.** BPF host routing requires BPF masquerading
([`tuning.rst` L197–200](https://github.com/cilium/cilium/blob/v1.20.1/Documentation/operations/performance/tuning.rst#L197-L200)),
and BPF masquerading turns it on by default
([`masquerading.rst` L82–86](https://github.com/cilium/cilium/blob/v1.20.1/Documentation/network/concepts/masquerading.rst#L82-L86)).
BPF host routing bypasses `netfilter` in the host's namespace
([`values.yaml` L708–716](https://github.com/cilium/cilium/blob/v1.20.1/install/kubernetes/cilium/values.yaml#L708-L716));
Talos' KubeSpan page says it "bypasses the kernel routing table and
conflicts with KubeSpan", and requires `bpf.hostLegacyRouting: true`
where BPF masquerading is on ([Talos KubeSpan](https://docs.siderolabs.com/talos/v1.13/networking/kubespan)); and Cilium's
Talos guide requires the same switch because Talos' host DNS forwarding
does not work with it ([Cilium: Talos L13](https://github.com/cilium/cilium/blob/v1.20.1/Documentation/installation/k8s-install-talos-linux.rst#L13)). This design
depends on `netfilter` in the host's namespace twice over: the node
firewall is an nftables chain (§4.3), and KubeSpan steers traffic into
its link by an nftables mark. So BPF masquerading would arrive with the
legacy switch both upstreams require of it, and without it no setting
chooses a legacy path: masquerading is the iptables implementation,
which Cilium calls the legacy one
([`masquerading.rst` L186–189](https://github.com/cilium/cilium/blob/v1.20.1/Documentation/network/concepts/masquerading.rst#L186-L189)),
and host routing goes through the stack because BPF host routing's
requirement is not met. Both families are masqueraded by default
([`masquerading.rst` L11–21](https://github.com/cilium/cilium/blob/v1.20.1/Documentation/network/concepts/masquerading.rst#L11-L21)),
which is still the IPv6 egress cluster-infra.md §2 gives the worker's
pods.

The one configuration with neither legacy path is Cilium's own
WireGuard in place of KubeSpan, which Talos' page offers for "advanced
eBPF features". It is rejected: KubeSpan is up before any CNI exists,
and day 1 reaches the worker through it, apid proxied over the mesh by
whichever control plane answers (physical.md §2), while Cilium's
WireGuard exists only once Cilium runs.

**The Egress Gateway is not enabled in M2.** It refuses to start without
BPF masquerading
([`manager.go` L190–211](https://github.com/cilium/cilium/blob/v1.20.1/pkg/egressgateway/manager.go#L190-L211)),
which brings the legacy switch above with it. The one workload that
uses it is hath, for its same-address egress (architecture.md §3.2),
and hath moves in the last wave, so M2 has none; the options for hath
are recorded in §15.3, with a recommendation that needs no Egress
Gateway at all.

### 4.2 The machine configuration's half

Three settings belong to the `physical` stack, because Talos renders
them:

*   **kube-proxy is disabled**: `cluster.proxy.disabled: true` in the
    control-plane configuration, which both Cilium's and Talos' guides
    set for an installation without kube-proxy
    ([Talos: Deploying Cilium](https://docs.siderolabs.com/kubernetes-guides/cni/deploying-cilium)).
    architecture.md §2.2 already states that kube-proxy is disabled, and
    the configuration does not do it (§10, finding 1).
*   **Host DNS forwarding is stated**:
    `machine.features.hostDNS.forwardKubeDNSToHost: true`. It is already
    on — `gen config` writes it for every contract newer than 1.7
    ([`init.go` L82–87](https://github.com/siderolabs/talos/blob/v1.13.9/pkg/machinery/config/generate/init.go#L82-L87))
    — but §4.5 depends on it, so it is written down rather than
    inherited.
*   **KubeSpan's MTU** from `conventions.KUBESPAN_MTU` (§4.1).

### 4.3 The host ports the node firewall opens

Every port on a node that Cilium listens on or answers through falls
into one of five classes. Cilium's own are read off the release's port
table
([`system_requirements.rst` L353–457](https://github.com/cilium/cilium/blob/v1.20.1/Documentation/operations/system_requirements.rst#L353-L457)),
and the Gateways' follow from how the datapath delivers their traffic:

| Class | Ports | What the firewall does |
| --- | --- | --- |
| Node to node | `vxlan` 8472/udp, cluster health 4240/tcp and ICMP echo | Nothing: the traffic is addressed to a peer's node address, so it rides KubeSpan and enters on `kubespan`, which the ingress chain accepts ahead of every rule ([`nftables_chain_config.go` L128–142](https://github.com/siderolabs/talos/blob/v1.13.9/internal/app/machined/pkg/controllers/network/nftables_chain_config.go#L128-L142)) |
| Local only | 4251, 6060–6062, 9878, 9879, 9890, 9891, 9893, 9901 | Nothing: they bind `127.0.0.1` or `::1` |
| Reached from a pod | the agent's metrics 9962, the operator's 9963, Envoy's 9964, Hubble's 9965 | **An opening from the cluster's own ranges**, the set the kubelet is opened to |
| A Gateway's listeners | the public port census's rows the Gateways answer: 80 and 443 today | **An opening from anywhere**, on every node |
| Not in use | the Hubble server 4244 (its one client is Relay, which is off), Relay 4245, mutual authentication 4250, Cilium's WireGuard 51871, `geneve` 6081 | Nothing |

**A Gateway's listener is a host socket.** For a Service marked as an
L7 load balancer, the datapath marks the packet for the proxy and
returns it to the host's stack, or hairpins it through `cilium_net`,
for Envoy to take
([`nodeport.h` L2730–2766](https://github.com/cilium/cilium/blob/v1.20.1/bpf/lib/nodeport.h#L2730-L2766));
Envoy runs on the host network; and the packet still carries the
listener's port. Cilium installs its own accept for marked proxy
traffic, "Needed when the INPUT defaults to DROP"
([`iptables.go` L831–840](https://github.com/cilium/cilium/blob/v1.20.1/pkg/datapath/iptables/iptables.go#L831-L840)),
but the Talos ingress chain is a base chain of its own on the input
hook, with drop as its policy in block mode
([`nftables_chain_config.go` L118–305](https://github.com/siderolabs/talos/blob/v1.13.9/internal/app/machined/pkg/controllers/network/nftables_chain_config.go#L118-L305)),
and an accept in another table does not undo a drop in it. A Gateway
port with no opening therefore drops every request, the balancer's
health check with them. Every other Service is different: the datapath
rewrites its packets to a pod ahead of the firewall, which is what
physical.md §2 says of every Service port and is true of these alone
(§10, finding 11).

**The Gateways' openings are derived from the census**, from the rows
the Gateways answer (§5.3), and never listed. They admit anywhere, on
every node shape. On a cloud node those ports are the internet's by
design, and nothing on the host binds them but through the proxy, so
the opening admits the Gateways' traffic and nothing else. On the
worker nothing from outside the site arrives at all: the site
gateway's only port forward is the bulk-transfer peer port, and no
management inbound exists ([physical/gateway.md](../physical/gateway.md)
§4.2), which is why the worker already carries the internet-facing
management openings unchanged. **New rule**, landing in physical.md §2.

**A host port a pod reaches is opened from the cluster's own ranges and
from nothing else**, the kubelet's rule applied to every such port.
Every Cilium process is on the host network
([`values.yaml` L3326–3327](https://github.com/cilium/cilium/blob/v1.20.1/install/kubernetes/cilium/values.yaml#L3326-L3327)
for the operator; the agent and Envoy hard-code it), so each metrics
port is a host port, and the scraper is a pod. The same class holds the
node exporter's 9100, which is on the host network in the monitoring
stack's pinned chart. The pod ranges are required rather than only the
node networks because a pod's request arrives from its own address
whether it crosses a tunnel to reach the node or not, which is why the
kubelet's opening names them already. **New rule**, landing in
physical.md §2 beside the kubelet's.

What the classes leave out is on purpose. Hubble's server is not opened,
so turning Relay on later is one opening in the same change. Nor is the
health check node port of a `Local` Service, which only a Service
holding an address alone can be (§4.4): nothing outside the node asks
it, since the balancer checks each listener's own port.

### 4.4 The pools

cluster-infra.md §2 fixes the two `CiliumLoadBalancerIPPool`s and their
members. What the design adds:

*   **The `internet` pool reads its members from `physical`'s outputs**,
    one block per address: each cloud node's primary private IPv4, each
    node's GUA, and the dedicated VIP's secondary private address. The
    `physical` stack exports no GUA today (§10, finding 5), so it gains
    an output for them in slice 2, named in
    `conventions.PHYSICAL_OUTPUTS` like every other.
*   **A Service asks for its addresses the way the release documents**:
    `lbipam.cilium.io/ips`, a comma-separated list, and
    `lbipam.cilium.io/sharing-key`; and because the Gateways and the raw
    TCP and UDP Services of the `internet` pool live in different
    namespaces and share the same node addresses, both sides carry
    `lbipam.cilium.io/sharing-cross-namespace`, without which sharing
    across namespaces is refused
    ([`lb-ipam.rst` L464–512](https://github.com/cilium/cilium/blob/v1.20.1/Documentation/network/lb-ipam.rst#L464-L512)).
*   **Every Service on the `internet` pool's node addresses takes the
    `Cluster` traffic policy.** LB IPAM shares an address only between
    Services whose traffic policies are equal, and between two `Local`
    Services only when both select the same pods
    ([`service_store.go` L88–139](https://github.com/cilium/cilium/blob/v1.20.1/operator/pkg/lbipam/service_store.go#L88-L139));
    a requested address that fails the check is refused as allocated to
    an incompatible Service
    ([`lbipam.go` L921–936](https://github.com/cilium/cilium/blob/v1.20.1/operator/pkg/lbipam/lbipam.go#L921-L936)).
    `internet-gw` is on those addresses and its Service selects nothing
    (§5.1), so a `Local` Service beside it, or a `Local` Gateway beside
    anything, would never be allocated. The dedicated VIP and each `lan`
    VIP are held by one Service, which keeps the choice
    architecture.md §3.1 gives. **New rule**, landing in architecture.md
    §3.1 and §3.2 and workloads.md §1.
*   **Pool membership is a Service label**, `conventions.LB_POOL_LABEL`,
    through `kluster.lib.k8s.lb_pool_labels`, as built.

The release documents nothing about a pool that holds node addresses,
which is why it stays a bootstrap verification (physical.md §6). Its
recorded fallback stays the reserved address per node, and it costs the
client's address. The balancer's IPv4 backends would then name those
addresses rather than the instances, whose backend stands for the
primary private address (`components/cloud/nodes.py`), and OCI refuses
an address-named backend in a set that preserves the source: "Preserve
source IP must be disabled in the backend set to add an IP
address-based backend server"
([OCI: Adding a backend server](https://docs.oracle.com/en-us/iaas/Content/NetworkLoadBalancer/BackendServers/create-backend-server.htm)).
Under the fallback, Envoy and every backend see the balancer's address
on IPv4 too, and the exit criterion's client-address check is given up
with it. Node address
allocation (`loadBalancerClass: io.cilium/node`) is not a second
fallback: it hands out node addresses only, so the dedicated VIP could
not be one of them
([`node-ipam.rst` L12–20](https://github.com/cilium/cilium/blob/v1.20.1/Documentation/network/node-ipam.rst#L12-L20)).

### 4.5 The baseline policy

cluster-infra.md §2 and architecture.md §4.1 have the cluster-wide
baseline deny pod egress to `169.254.0.0/16`, the range the OCI
metadata service is in. Two facts change its shape:

*   **The range holds the address pods resolve names through.** With
    host DNS forwarding on, the cluster DNS service forwards to
    `169.254.116.108`, an address Talos allocates for its own resolver
    ([Talos host DNS](https://docs.siderolabs.com/talos/v1.13/networking/host-dns)),
    and the ingress chain admits the pod and service ranges to it on
    port 53 ([`nftables_chain_config.go` L210–240](https://github.com/siderolabs/talos/blob/v1.13.9/internal/app/machined/pkg/controllers/network/nftables_chain_config.go#L210-L240)).
    The host path is kept because it is Talos' default, and it conflicts
    only with BPF host routing, which this design does not run (§4.1). A
    cloud node's own resolver would be no alternative inside the range:
    Talos' `oracle` platform gives every cloud node the VCN's resolver,
    `169.254.169.254`
    ([`metadata.go` L25](https://github.com/siderolabs/talos/blob/v1.13.9/internal/app/machined/pkg/runtime/v1alpha1/platform/oracle/metadata.go#L25),
    [`oracle.go` L102–105](https://github.com/siderolabs/talos/blob/v1.13.9/internal/app/machined/pkg/runtime/v1alpha1/platform/oracle/oracle.go#L102-L105);
    [OCI: DNS in a VCN](https://docs.oracle.com/en-us/iaas/Content/Network/Concepts/dns.htm)),
    the metadata service's own address. **The deny is `169.254.0.0/16`
    except `169.254.116.108/32`** (§10, finding 2).
*   **A deny rule alone switches every selected endpoint to
    default-deny.** When `enableDefaultDeny` is unset, a policy with
    egress deny rules sets it for egress
    ([`rule.go` L118–136](https://github.com/cilium/cilium/blob/v1.20.1/pkg/policy/api/rule.go#L118-L136)),
    so a cluster-wide baseline written that way denies every pod all
    egress. The baseline sets `enableDefaultDeny` false in both
    directions, which leaves default-deny to the per-namespace policies
    that are meant to impose it (workloads.md §1).

### 4.6 BGP to the gateway

cluster-infra.md §2 fixes the session: BGPv2, the worker only, both
families, the `lan` pool's addresses as host routes, and an MD5
password on both ends. The release has no BGPv1 resources left to avoid.
What remains is placement:

*   The cluster configuration (`CiliumBGPClusterConfig`) selects the
    homelab worker, the peer configuration (`CiliumBGPPeerConfig`)
    carries the password, and the advertisement
    (`CiliumBGPAdvertisement`) advertises `LoadBalancerIP` for the
    Services the `lan` pool label selects
    ([`bgp-control-plane-configuration.rst` L660–690](https://github.com/cilium/cilium/blob/v1.20.1/Documentation/network/bgp-control-plane/bgp-control-plane-configuration.rst#L660-L690)).
*   The password is a Secret in `kube-system` under the key `password`,
    the release's default secrets namespace
    ([`values.yaml` L515–523](https://github.com/cilium/cilium/blob/v1.20.1/install/kubernetes/cilium/values.yaml#L515-L523)),
    produced by a SealedSecret whose value `credentials derived bgp
    record` seals alongside the copy it already writes for the site
    gateway (§6.2).
*   The session lands in a slice after the controller is running,
    because its password cannot be sealed before (§3.2). Nothing on the
    LAN side needs it earlier: the first LAN-side route is not an M2
    route (§9.3).

--------------------------------------------------------------------------------

## 5. The front door

### 5.1 The Gateways' Service, and its traffic policy

**Cilium runs one Envoy per node, and sends Gateway traffic to the Envoy
on the node it arrives at, before any backend is chosen.** The release
says so in its reference ("all ingress traffic bound for a Service that
exposes Envoy is *always* going to the local node"), and says that under
either policy the traffic is forwarded "while keeping the source IP
intact"
([`ingress-reference.rst` L97–145](https://github.com/cilium/cilium/blob/v1.20.1/Documentation/network/servicemesh/ingress-reference.rst#L97-L145)).
The datapath is the evidence: for a Service marked as an L7 load
balancer, the handoff to the local proxy comes ahead of backend
selection
([`nodeport.h` L2724–2790](https://github.com/cilium/cilium/blob/v1.20.1/bpf/lib/nodeport.h#L2724-L2790)), and it
goes through the host's stack, which is why a Gateway's port is an
opening in the node firewall (§4.3). A Gateway's Service has no
selector and one placeholder endpoint, which is never a backend traffic
reaches
([`translator.go` L383–431](https://github.com/cilium/cilium/blob/v1.20.1/operator/pkg/model/translation/gateway-api/translator.go#L383-L431)).

So the design's picture of the Gateways does not hold under this
implementation (§10, finding 3). architecture.md §3.3 has `internet-gw`
as Envoy replicas across the cloud nodes and `lan-gw` and `media-gw` as
Envoys pinned to the worker, all under the `Local` policy, so that
client addresses reach Envoy. What is true instead:

*   **Where a Gateway answers is decided by where its address is
    routed**, not by where an Envoy runs. `internet-gw`'s addresses are
    the cloud nodes' own, so the balancer decides it; `lan-gw`'s and
    `media-gw`'s are announced by the worker alone (§4.6), so only the
    worker receives them.
*   **The client's address survives under either policy.** The traffic
    policy decides nothing for a Gateway's Service except whether it can
    share an address and whether Kubernetes allocates it a health check
    node port.

**The Gateways' Services take the `Cluster` policy**, for two reasons,
either sufficient:

*   **`Local` could not be allocated.** `internet-gw` shares the cloud
    nodes' addresses with the raw Services of the public port census,
    and LB IPAM shares an address only between Services of one policy,
    and between two `Local` Services only when both select the same
    pods, which a Gateway's Service, selecting none, never does (§4.4).
    The design's `Local` Gateway on shared node addresses is one that
    could never have been given them (§10, finding 12).
*   **`Local` adds a listener on every node.** Kubernetes allocates it a
    health check node port whatever `allocateLoadBalancerNodePorts`
    says — that field gates only the per-port node ports
    ([`alloc.go` L498–523](https://github.com/kubernetes/kubernetes/blob/v1.36.3/pkg/registry/core/service/storage/alloc.go#L498-L523),
    at the Kubernetes release Talos 1.13.9 ships) — and the agent serves
    it on every node address from a server of its own, which answers
    healthy on every node for a Service whose traffic goes to a proxy
    ([`healthserver.go` L324–331](https://github.com/cilium/cilium/blob/v1.20.1/pkg/loadbalancer/healthserver/healthserver.go#L324-L331)).
    That is one more listener on each node's public address, answering
    nothing anyone asks.

The raw TCP and UDP Services on the node addresses take `Cluster` for
the first reason, and their backends see the receiving node's address,
the cost architecture.md §3.1 already records for `Cluster`.

**Both properties are stated in the class configuration, and neither
is left to a default.** The Gateways share one `GatewayClass` whose
`parametersRef` names a `CiliumGatewayClassConfig` carrying
`spec.service.externalTrafficPolicy: Cluster`,
`allocateLoadBalancerNodePorts: false` (the rule already built), and both
IP families required
([`gatewayclassconfig_types.go` L86–140](https://github.com/cilium/cilium/blob/v1.20.1/pkg/k8s/apis/cilium.io/v2alpha1/gatewayclassconfig_types.go#L86-L140)).
The configuration's own default for the policy is `Cluster`, and the
translator takes the configuration's value over the one the chart sets
whenever the configuration sets one
([`translator.go` L284–297](https://github.com/cilium/cilium/blob/v1.20.1/operator/pkg/model/translation/gateway-api/translator.go#L284-L297)),
so a configuration that states only the node-port field changes the
policy too. Stating both is what makes it a decision rather than a side
effect.

**Each Gateway asks for its addresses in its own spec.** `spec.addresses`
becomes the address annotation of §4.4, and `spec.infrastructure` labels
and annotations are copied onto the generated Service
([`gateway.go` L155–178](https://github.com/cilium/cilium/blob/v1.20.1/operator/pkg/model/ingestion/gateway.go#L155-L178)),
which is how the pool label and the sharing annotations reach it.
`internet-gw` names the `internet` pool's node members, `lan-gw` the
`lan` pool's default VIP and `media-gw` its media VIP, both literals in
`conventions.LAN_POOL` already.

**What a cloud node does with a Gateway's node port** is then answered
by removal: the Service has none and no health check port. The bootstrap
gate measures what the source says, since none of it has run
(§14, slice 13): no Gateway Service carries a `nodePort` or a
`healthCheckNodePort`; a request answers on 443 through the balancer and
an undeclared port at a node's public address does not; and Envoy's
access log records the client's address through the balancer's IPv4
front. Through the IPv6 front, a request answers at all — the Service's
placeholder endpoint is IPv4 alone
([`translator.go` L410](https://github.com/cilium/cilium/blob/v1.20.1/operator/pkg/model/translation/gateway-api/translator.go#L410)),
so the gate asks — and Envoy records the client's address only where
the balancer's IPv6 sets kept source preservation. Each IPv6 backend
names a GUA, which OCI refuses in a set that preserves the source, and
physical.md §6's recorded fallback turns preservation off on the IPv6
sets. Under that fallback the IPv6 front records the balancer's address
by design: the cost physical.md §6 accepts for the apiserver's audit
log, accepted here for Envoy's too. **There is no traffic-policy
fallback.** `Local` could not be allocated beside the raw Services and
would not move the handoff to the proxy, so a client address lost on the
IPv4 front is a defect of the datapath or of the balancer, filed as its
own issue; the balancer's own fallback is architecture.md §3.2's.

### 5.2 Listeners and certificates

*   **One certificate per served zone, for the apex and the wildcard
    together**, the shape [dns.md](../declarative/dns.md) §4 gives the
    cluster's issuance. The served zones are derived: every zone a row
    of the route census names. The derivation is one function in
    `conventions.routes`, because two programs read it — `k8s-base`,
    which declares the certificates, and the `credentials` command,
    which scopes the DNS-01 token to the same zones (§6.2). A row in a
    new zone is then a new certificate in the same preview that shows
    the row. **New rule**, landing in dns.md §4 and cluster-infra.md §2.
*   **One `ClusterIssuer`, Let's Encrypt over DNS-01**, whose Cloudflare
    token is a Secret in cert-manager's own namespace, the one a cluster
    issuer's credentials are read from under the chart's defaults
    ([`deployment.yaml` L100–104](https://github.com/cert-manager/cert-manager/blob/v1.21.1/deploy/charts/cert-manager/templates/deployment.yaml#L100-L104)).
    cert-manager's Gateway integration stays off: a `Certificate`
    needs none of it.
*   **The certificates live in the Gateways' namespace**,
    `conventions.GATEWAY_NAMESPACE`, so no listener refers across a
    namespace.
*   **A renewed certificate reaches the listeners without a restart.**
    A Gateway's listener names its certificate as an SDS secret
    ([`envoy_listener.go` L896–913](https://github.com/cilium/cilium/blob/v1.20.1/operator/pkg/model/translation/envoy_listener.go#L896-L913)).
    The operator copies a referenced TLS Secret into Cilium's secrets
    namespace and reconciles the copy whenever the source changes
    ([`secretsync.go` L110–112](https://github.com/cilium/cilium/blob/v1.20.1/operator/pkg/secretsync/secretsync.go#L110-L112),
    [`secretsync_reconcile.go` L175–207](https://github.com/cilium/cilium/blob/v1.20.1/operator/pkg/secretsync/secretsync_reconcile.go#L175-L207)),
    and the agent pushes every Secret in that namespace to Envoy over
    SDS when it is created or updated
    ([`pkg/envoy/secretsync.go` L61–108](https://github.com/cilium/cilium/blob/v1.20.1/pkg/envoy/secretsync.go#L61-L108)).
    The sync is on by default
    ([`values.yaml` L1125–1133](https://github.com/cilium/cilium/blob/v1.20.1/install/kubernetes/cilium/values.yaml#L1125-L1133)),
    and the values state it, so cert-manager's renewal of a
    certificate's Secret is served with nothing restarted. Slice 13
    forces a renewal and checks the served certificate.
*   **Every Gateway has an HTTPS listener per served zone**, on the
    wildcard hostname with that zone's certificate, and one HTTP
    listener whose only route is a redirect to HTTPS. Their ports are
    the census rows the Gateways answer (§5.3), the same rows the node
    firewall opens (§4.3). The apex listener arrives with the first
    application served at an apex, which is the website.
*   **A route attaches from a namespace that carries the route label**,
    one label in `conventions.routes` that `allowedRoutes` selects on
    and that the component declaring a route sets on its namespace. An
    `HTTPRoute` in any other namespace — one a chart installs, say —
    attaches to nothing.

### 5.3 The balancer's listeners for the public port census

**The `physical` stack declares a listener and a backend set per family
for every row of the public port census that the balancer fronts.** Only
`physical` can: the credential partition gives `apps` no OCI credential
([framework/ci.md](../framework/ci.md) §3,
[credentials.md](../credentials.md) §3), and `public_port` was designed
to emit the listener from `apps` anyway (§10, finding 4). The census
becomes the source of every public port's listener, beside being the
reference an audit reads for which ports are public.

The census is read by three programs from here on — `physical` for the
listeners and the Gateways' openings, `k8s-base` for the Gateways'
ports, `apps` for the raw Services — and it stays a convention. Its rows
gain what those readers need:

*   **A name**, which the listener and the backend set are named after,
    by the rule the management ports already follow: after the row and
    never after its number, and after the row and the family for IPv6.
    The names share the balancer with the management ports', so a test
    holds the two sets disjoint.
*   **Its transports**, which set the listener's protocol: `TCP`, `UDP`,
    or `TCP_AND_UDP` for a port served on both, which one listener
    carries
    ([OCI: Managing listeners](https://docs.oracle.com/en-us/iaas/Content/NetworkLoadBalancer/Listeners/listener-management.htm)).
*   **Its front**: the balancer, or the dedicated VIP. hath's row is the
    VIP's, and a listener for it would forward to three nodes none of
    which answers on that port at its primary address.
*   **What answers it**: the Gateways, whose Envoy takes the traffic on
    the host (§4.3), or a Service's pods, which the datapath reaches
    ahead of the firewall. The Gateways' rows are the ports of their
    listeners and of their node openings.

**Every census backend set preserves the client's address**
(`is_preserve_source`), as the management sets do
(`components/cloud/nodes.py`): §5.1's client-address argument and the
exit criterion both rest on it on IPv4. Its IPv6 half inherits the
fallback physical.md §6 records for the management sets' IPv6 backends,
which name the nodes' GUAs, and with it that fallback's cost: the
balancer's address in place of the client's (§5.1).

The backend set checks the row's port over TCP. A UDP check needs a
request the application answers with a known reply
([OCI: health checks](https://docs.oracle.com/en-us/iaas/Content/NetworkLoadBalancer/HealthCheckPolicies/health-check-policy-management.htm)),
and no UDP-only row exists, so the component refuses one until a row
can say what to send. The balancer's limit of fifty listeners and fifty
backend sets
([OCI: NLB limits](https://docs.oracle.com/en-us/iaas/Content/NetworkLoadBalancer/introduction.htm#LimitsNLBResources))
is a census invariant: twice the count of management ports and balancer
rows fits under it.

**Adding a public port is a census row and a gated `physical` apply.**
That is the cost of the partition, and it is paid rarely: the census is
the handful of rows `conventions.PUBLIC_PORT_CENSUS` holds today.
`public_port` keeps the Service half — the LoadBalancer Service on the
`internet` pool, from the row it is handed, under `Cluster` and without
node ports — and loses the listener. Weighed and rejected:

*   **An OCI credential for `apps`**, scoped to the balancer. It would
    also reach the management listeners, and it reverses the partition
    the security audit's H3 finding asked for.
*   **No balancer for workload ports**, with public records naming the
    three node addresses directly. That is the recorded fallback for a
    balancer that fails its bootstrap verification (architecture.md
    §3.2), and it gives up the health-checked failover the balancer is
    there for.

--------------------------------------------------------------------------------

## 6. Secrets in the cluster

### 6.1 The controller

The sealed-secrets chart installs into `kube-system` under the name
`sealed-secrets-controller`, the two defaults `kubeseal` assumes
([`main.go` L85–90](https://github.com/bitnami-labs/sealed-secrets/blob/v0.39.1/cmd/kubeseal/main.go#L85-L90));
the chart names it `sealed-secrets` unless told otherwise, so
`fullnameOverride` is set. The controller keeps its defaults otherwise,
key renewal every thirty days included: renewal adds a key and keeps
the old ones, so a committed ciphertext stays readable
([`README.md` L617–621](https://github.com/bitnami-labs/sealed-secrets/blob/v0.39.1/README.md#L617-L621)).
The key is not escrowed, as credentials.md §2.2 already decides.

### 6.2 Where a sealed value lives, and what writes it

**A sealed value is a plain value in the configuration of the stack
that declares it**, under one structured key, `sealedSecrets`, keyed by
the value's name and then by its data key. `kubeseal`'s ciphertext opens
with the cluster's sealing key alone, so it needs no stack encryption
and is committed in the clear beside the stack's config secrets. The
stack program reads the key and hands each component the ciphertext of
the values it declares — configuration read at the layer that owns it
(style/pulumi.md, "Layering") — and the component hands it to
`sealed_secret`, which exists. **New rule**, landing in credentials.md
§1 (rule 6, the SealedSecret channel) and cluster-infra.md §1.1.

**The sealed values are a census**, because two programs agree on each
one: the `credentials` command writes it and a stack declares it.
`conventions.sealed` lists each by name, namespace, the keys of its
data, the scope it is sealed at and the stack that declares it, and the
configuration path it lives at is derived from the row. A value
`credentials` seals is sealed strict — name and namespace bound into the
ciphertext, `kubeseal`'s own default — since nothing about it will be
renamed; the `namespace-wide` default of `sealed_secret` stays for the
legacy manifests it was chosen for. **New rule**, landing in
credentials.md §3 and cluster-infra.md §1.1.

**`credentials` seals through the pinned `kubeseal`**, with the
controller's certificate fetched through the API server's service proxy,
the path `kubeseal` itself uses
([`kubeseal.go` L141–146](https://github.com/bitnami-labs/sealed-secrets/blob/v0.39.1/pkg/kubeseal/kubeseal.go#L141-L146)),
over the kubeconfig in `physical`'s state, and writes the ciphertext
into the declaring stack's configuration with `pulumi config set
--path`, the CLI the package already drives for config secrets
(`credentials/pulumi_config.py`). Sealing is a hybrid encryption format
of the controller's own; writing it in Python would be hand-rolled
cryptography, and `kubeseal` pinned in `mise.toml` is the tool built for
it. A mint seals in the same run that mints, so no value is parked
(credentials.md §1, rule 2). The M2 instances, each in `k8s-base`'s
configuration:

| Value | Namespace | Written by |
| --- | --- | --- |
| The DNS-01 token (§5.2) | cert-manager's | `credentials derived cloudflare-dns01 mint` |
| The BGP password (§4.6) | `kube-system` | `credentials derived bgp record` |
| alertmanager's webhook (§7.3) | the monitoring namespace | a new `record` row |

**The DNS-01 token cannot be scoped to challenge records.** Cloudflare
scopes a token's permissions to zones and not to records within one
([Cloudflare: Create API token](https://developers.cloudflare.com/fundamentals/api/get-started/create-token/)),
and cert-manager documents DNS edit and zone read
([cert-manager: Cloudflare](https://github.com/cert-manager/website/blob/37b1d474676c6d61ec791e087afeffbbf22c56ff/content/docs/configuration/acme/dns01/cloudflare.md#L12-L47)).
So the token is minted with the permissions the gateway's own ACME
token carries, on the served zones of §5.2, and its register row records
the excess rule 4 asks for (§10, finding 9). The B2 writer keys of the
same register section wait for their consumers, which arrive with
`backed_pvc` and the first database; nothing in M2 reads them.

### 6.3 The legacy key

migration.md §1 restores the legacy cluster's sealing key when
`k8s-base` comes up. It moves to the first ported legacy manifest
instead, at the head of the first wave, because M2 ports none and needs
none. Importing it later costs M2's values nothing: the controller keeps
every key it holds, so what it opened before the import it opens after.
What the import can change is which key seals, since the controller
seals with the key whose certificate is newest
([`keyregistry.go` L69–101](https://github.com/bitnami-labs/sealed-secrets/blob/v0.39.1/pkg/controller/keyregistry.go#L69-L101)),
and the legacy controller renews its key too. So the wave that imports
it confirms afterward that the certificate the controller serves is the
cluster's own. How the key reaches the new cluster is also that wave's:
it is a secret moved by hand from the legacy cluster, and credentials.md
§1 has no channel for one yet.

--------------------------------------------------------------------------------

## 7. Monitoring and the alert legs

### 7.1 What is installed, and where it runs

The monitoring component installs the VictoriaMetrics stack chart and
metrics-server, and runs on the homelab worker. The push leg reaches the
phone through the home system anyway, so a home site that is down
silences it wherever the spine runs; the worker has the memory and the
local disk, and the cloud nodes share their cores with etcd. The
database's volume is `local-path` and unbacked, with the reason on
record: monitoring is rebuilt fresh, not migrated (migration.md §3).
The chart's Grafana runs, as item 6 of cluster-infra.md §1 lists it,
and is reached by a port-forward until its name moves (§7.4). The
namespace is `privileged`, for the node exporter (§8).

Four of the chart's defaults are switched off:

*   **The sync job**, which fetches rules and dashboards "from upstream
    sources at deploy time", off the upstream projects' `master`
    branches
    ([`values.yaml` L106–127, L161–165](https://github.com/VictoriaMetrics/helm-charts/blob/victoria-metrics-k8s-stack-0.91.2/charts/victoria-metrics-k8s-stack/values.yaml#L106-L165)).
    What a run installs would then depend on the day it ran, which is
    the drift [operations.md](../operations.md) §1 calls every pin
    without an opener.
*   **The default rules**, which have no playbooks here and so may not
    ship (§7.2).
*   **The default dashboards**, which arrive by the same download.
    Dashboards are declared by the components that own them, under the
    `grafana_dashboard` label (workloads.md §1).
*   **The scrape jobs for the controller manager, the scheduler and
    etcd.** Talos binds the first two to `127.0.0.1`
    ([`control_plane_static_pod.go` L583–618](https://github.com/siderolabs/talos/blob/v1.13.9/internal/app/machined/pkg/controllers/k8s/control_plane_static_pod.go#L583-L618))
    and gives etcd no metrics address apart from its mutually
    authenticated client port
    ([`spec.go` L158–194](https://github.com/siderolabs/talos/blob/v1.13.9/internal/app/machined/pkg/controllers/etcd/spec.go#L158-L194)),
    so the jobs would scrape nothing and alert on their own absence.
    Exposing them is three `extraArgs` and three openings of §4.3's
    class, and it lands with the first rule that needs them.

The scrapes that do run: the kubelet and its containers, the API server,
the cluster DNS, the node exporter, kube-state-metrics, and Cilium's
four metrics endpoints. vmagent keeps the chart's `selectAllByDefault`
and reads every VictoriaMetrics scrape object in the cluster
([`values.yaml` L1978–1981](https://github.com/VictoriaMetrics/helm-charts/blob/victoria-metrics-k8s-stack-0.91.2/charts/victoria-metrics-k8s-stack/values.yaml#L1978-L1981)),
so no selector label is required of a component that declares one
(§10, finding 10).

### 7.2 The rules M2 ships, and what a rule carries

**Every rule this repository declares carries a `tier` label from
`conventions.alert.Tier` and `summary` and `playbook` annotations**, the
fields the dispatch payload carries, so an alert reads the same whatever
path it took (architecture.md §4.3). A test holds every declared rule to
that: a tier the census defines, a summary, and a playbook that is a
document and a section the documentation sweep resolves. That turns the
playbook rule — "an automation alert whose response procedure is not
written down is not shipped" — into a gate rather than a review
question. **New rule**, landing in architecture.md §4.3 and
operations.md §5.

M2 ships these, each with its playbook row:

| Rule | Tier | Playbook |
| --- | --- | --- |
| alertmanager's notifications are failing (`alertmanager_notifications_failed_total` increasing, [`notify.go` L315–332](https://github.com/prometheus/alertmanager/blob/v0.32.1/notify/notify.go#L315-L332)) | actionable | architecture.md §4.3, the alert channel's failure |
| A backup is older than its class allows (the backup-freshness family, cluster-infra.md §3) | actionable | storage.md §5 |
| A node is not ready | actionable | operations.md §3 |
| The cluster's heartbeat (§7.3) | heartbeat | operations.md §4, the dead-man |

The freshness family evaluates over no series until the first backup
exists, which is the state it is designed to start from. The
authentication canary of cluster-infra.md §2 waits for the
authentication service, which is an application.

### 7.3 The push leg, and the dead-man

**alertmanager posts to a webhook of its own.** The intake the operations
repository posts to takes that repository's payload, with the `push`
flag the automation branches on (operations.md §4), and alertmanager's
body is fixed (architecture.md §4.3), so it cannot post there. It posts
to a second automation that reads the tier, the summary and the
playbook off each alert's labels and annotations, and pushes under the
same title convention. Both automations end at the same phone
notification, which is what architecture.md §4.3 means by one channel.
The webhook's address is a sealed value (§6.2), handed to the receiver
through the VictoriaMetrics operator's `url_secret` field, so it appears
in no rendered configuration
([`vmalertmanagerconfig_types.go` L533–563](https://github.com/VictoriaMetrics/operator/blob/v0.74.0/api/operator/v1beta1/vmalertmanagerconfig_types.go#L533-L563)).
The credential register gains a row for it and the existing webhook row
loses alertmanager as a consumer (§10, finding 8).

**The cluster checks in.** A rule that always fires, in the `heartbeat`
tier, is routed to that webhook on a short repeat, and it restarts a
Home Assistant timer whose expiry pushes "no check-in from the cluster".
That is operations.md §4's dead-man applied to the cluster: without it,
a cluster that stops evaluating rules is silent until the operations
repository's poller exists and its unreachable count trips.
**New rule**, landing in operations.md §4.

What M2's exit criterion checks is the push itself: an alert raised
through alertmanager's API reaches the phone under the title convention,
and stopping vmalert pushes the missed check-in (§14, slice 13).

### 7.4 The issue leg, and the dashboard's name

The issue leg is the operations repository's poller reading
alertmanager's alert list through a route matched on method, path and a
bearer header (cluster-infra.md §1). The poller does not exist, so a
route declared now would be a public surface with no reader. Both land
together, when the dashboard's name moves: the legacy `mon` block stays
the dashboard's until monitoring moves in its wave (migration.md §2),
and the read route needs a public name. What is settled here, and what
that wave then builds:

*   **Monitoring is `k8s-base`'s**, dashboard included, as item 6 of
    cluster-infra.md §1 says. rfc-003 §20 left open which component
    declares `mon`; it is the monitoring component.
*   **A component's routes are its own**, declared through the same
    `route(row)` helper every application calls (§9.1). cluster-infra.md
    §3's "no ingress of its own" means the stack declares no
    application's route, and the monitoring component's routes are not
    an application's (§10, finding 7).

Which name the read route answers on, and whether `k8s-base` holds the
zones token to publish a record for it, are that wave's (§15.3).

--------------------------------------------------------------------------------

## 8. The rest of the closed list

**Every namespace `k8s-base` creates states its Pod Security level.**
Talos enforces `baseline` cluster-wide and exempts `kube-system` alone
([`init.go` L106–129](https://github.com/siderolabs/talos/blob/v1.13.9/pkg/machinery/config/generate/init.go#L106-L129)),
and `baseline` refuses host networking, the host PID namespace and host
paths, which several entries need. So each namespace the stack creates
carries its `enforce` label: `restricted`, the level workloads.md §1
gives an application, unless its component's pods need the host, and
then `privileged` with the reason in the component. Today's
`privileged` namespaces are the monitoring one, for the node exporter's
host network, PID namespace and paths; local-path-provisioner's, whose
helper pods mount the host path they provision; and NFD's and the GPU
plugin's, for their device and host mounts. What installs into
`kube-system` — Cilium and the sealing controller — stays under Talos'
exemption. A chart whose pods need no host access and still do not
meet `restricted` has its security-context values set to meet it where
the chart exposes them, and its namespace takes `baseline`, with the
reason, only where it does not. reloader is the instance at its pin:
its pod context is `restricted`-shaped, and its container context
(`reloader.deployment.containerSecurityContext`) is empty, with the
capabilities drop, the privilege-escalation refusal and the read-only
root filesystem written as comments in the chart's own values, which
the component sets.
**New rule**, landing in cluster-infra.md §0.

Install steps, each a component area of its own, in cluster-infra.md
§1's order:

*   **cert-manager**: definitions on (`crds.enabled`, off by default in
    the chart) and the start-up check off, as cluster-infra.md §1.2
    decides; the issuer and certificates of §5.2 follow once the token is
    sealed.
*   **CNPG** and the barman plugin, each from its pin, and
    **VolSync**. Both install with no secret. The backups that use them,
    and the writer keys those need, arrive with the first database and
    the first `backed_pvc`.
*   **NFD and the Intel GPU plugin**, inert until the worker's GPU is
    bound, present from the start so that step needs no `k8s-base`
    change (item 7 of cluster-infra.md §1).
*   **local-path-provisioner**, declared as resources of this program,
    in the shape the legacy cluster already runs: a `ServiceAccount`
    with its `ClusterRole` and binding; a `ConfigMap` carrying the
    provisioner's configuration — `conventions.LOCAL_PATH_ROOT` as the
    path of the `local-path` class — and the template and the setup and
    teardown scripts of the helper pod the provisioner starts on a node
    to create and remove a volume's directory, rendered from files beside
    the component (rfc-002 §9.1); a `Deployment`; and the `local-path`
    `StorageClass`, named from `conventions.SC_LOCAL_PATH`, the cluster's
    default, reclaiming `Delete` and binding when a pod is scheduled
    (storage.md §2 and §3.3). Its pins are two images, the provisioner's
    and the helper pod's, as `versions:image-<name>` keys: no chart and
    no manifest. Its upstream publishes no chart repository — its chart
    is installed from a clone of its repository
    ([its chart's README at v0.0.37](https://github.com/rancher/local-path-provisioner/blob/v0.0.37/deploy/chart/local-path-provisioner/README.md))
    — and the few objects it is are clearer declared than fetched.
*   **metrics-server**, installed by the monitoring component (§7.1),
    and **reloader** on its own.

--------------------------------------------------------------------------------

## 9. `apps`: the first application

### 9.1 The component base, and where the route helper lives

The `apps` area gains the base every application component extends: it
creates the namespace with Pod Security `restricted` enforced, the route
label of §5.2 and a default-deny network policy, and it admits traffic
from the Gateways. Cilium gives traffic that arrives at a Gateway's
Envoy the `ingress` identity
([`ingress-reference.rst` L28–56](https://github.com/cilium/cilium/blob/v1.20.1/Documentation/network/servicemesh/ingress-reference.rst#L28-L56)),
so a namespace's policy admits the `ingress` entity and nothing else
from outside it.

**`route(row)` lives in `kluster.lib.k8s`**, not on the base, because
two stacks call it: every application, and the monitoring component
(§7.4). It emits what dns.md §5 says — the `HTTPRoute` on the gateways
the row's exposure selects, and the public records across the row's
zones where the row publishes one — and a stack that calls it for a row
with a public record builds a Cloudflare provider and reads `dns`'s zone
identifiers. `public_port` stays with the base, since only applications
publish raw ports, and emits the Service alone, under `Cluster` (§4.4,
§5.3). Each is written
when an application first needs it: M2's needs `route` and not
`public_port`.

### 9.2 The front-door check

The first component is a small echo server whose only job is to be the
exit criterion: a public row in the primary zone, off Cloudflare's
proxy, so that the request crosses the balancer with the client's
address rather than Cloudflare's, on an image pinned by digest through
`versions:image-<name>` that runs non-root under the `restricted`
policy and echoes the request's headers. It proves the whole chain once:
the public record, the balancer's listener, the node's frontend, the
Gateway's Envoy, the route, the certificate and the pod. It is deleted in
the pull request that lands the first public application of the first
wave, together with its row, because a name that resolves is a promise
(dns.md §2).

### 9.3 The zone set of a route with no public record

A `LAN_ONLY` or `IOT` row that names no zone publishes into the
primary alone, which is what `Route.zones` defaults to for every row.
There is no default per exposure. Three reasons, each sufficient:

*   **The zone decides which wildcard certificate covers the name**, and
    the primary's is the one the cluster holds anyway, since it serves
    public names there (§5.2). Any other zone would be a certificate
    bought for a name no public resolver answers, which is the cost
    rfc-003 §6.2 weighed.
*   **The primary is the only zone that can hold an application name.**
    Every application behind forward authentication shares one cookie
    domain and one portal address, and every one using OpenID Connect
    registers its redirect addresses against a hostname (dns.md §2); a
    LAN-only name in another zone is a login that loops.
*   **The rewrite already follows the row.** `dns` writes one rewrite
    per zone the row names, in both families, at the VIP the exposure
    selects, so the default needs nothing new on that side.

A row that names another zone still may, in the open, as dns.md §2
allows; §5.2 then issues that zone's certificate. The first LAN-side
route is not an M2 route: the dashboard is the first candidate, in its
wave (§7.4).

--------------------------------------------------------------------------------

## 10. Where the canon contradicts itself or the pinned releases

Each finding names both sites. The slice that repairs each is in §14;
none needs a ruling beyond this document.

1.  **kube-proxy.** architecture.md §2.2 says kube-proxy is disabled,
    and cluster-infra.md §2 says there is none to fall back on. The
    control-plane configuration sets the CNI to `none` and says nothing
    about the proxy (`control_plane_patch` in
    `src/kluster/components/talos/__init__.py`), physical.md §2's list of
    what the patches carry is silent on it too, and Talos runs
    kube-proxy unless told not to. Repaired by slice 1.
2.  **The metadata deny and DNS.** cluster-infra.md §2 and
    architecture.md §4.1 deny pods all of `169.254.0.0/16`; the address
    pods resolve through, and the cloud nodes' own resolver, are both in
    it (§4.5). Repaired by slice 5.
3.  **The Gateways' Envoys and traffic policy.** architecture.md §3.3
    and cluster-infra.md §2 place Envoy replicas on chosen nodes under
    `Local`; the pinned Cilium runs one Envoy per node and redirects to
    it before choosing a backend (§5.1). Repaired by slice 5.
4.  **Who declares a workload's listener.** physical.md §1,
    workloads.md §1 and dns.md §5 have `public_port`, in `apps`, emit the
    balancer's listeners; framework/ci.md §3 and credentials.md §3 give
    `apps` no OCI credential (§5.3). Repaired by slice 2.
5.  **The `internet` pool's inputs.** cluster-infra.md §0 and §2 build
    the pool from `physical`'s outputs, the node GUAs among them;
    physical.md §0 and `conventions.PHYSICAL_OUTPUTS` export no GUA.
    Repaired by slice 2.
6.  **The chart register.** Item 8 of cluster-infra.md §1 installs
    local-path-provisioner; `update_crds/pins.py` calls itself a
    complete register of what the stack installs and has no entry for
    it. Repaired by slices 4 and 6: the chart pins move to the
    `versions:` block, and local-path-provisioner is declared with its
    images pinned there (§3.4, §8).
7.  **Monitoring's home.** Item 6 of cluster-infra.md §1 puts the
    dashboard and the alert read route in `k8s-base`; cluster-infra.md
    §3 says routes arrive with `apps`; migration.md §2 moves monitoring
    in an application wave. Settled in §7.4: the component is
    `k8s-base`'s, and the wave moves its name. Repaired by slice 11's
    documents.
8.  **The Home Assistant webhook.** credentials.md §3 names alertmanager
    a consumer of the webhook the operations repository's handler posts
    to; operations.md §4 fixes that webhook's body to the handler's
    payload, and architecture.md §4.3 says alertmanager's body is fixed.
    Settled in §7.3. Repaired by slice 8.
9.  **The DNS-01 token's scope.** credentials.md §3 scopes it to
    `_acme-challenge` edits; Cloudflare scopes a token to zones, not to
    records (§6.2). Repaired by slice 8.
10. **How scrape targets are selected.** workloads.md §1 has components
    carry the `release` label "so VictoriaMetrics picks them up", which
    is the label-selection convention of the legacy installation's
    operator; cluster-infra.md §1 has this cluster declare only
    VictoriaMetrics scrape objects. vmagent reads all of them (§7.1).
    Repaired by slice 12, which owns that document.
11. **Service ports and the node firewall.** physical.md §2 says every
    Service port is answered by Cilium's datapath ahead of the firewall,
    so machine configuration carries none, and the Talos component's
    comment on its openings says the same; for a Gateway's listeners the
    datapath hands the packet to the host's stack for Envoy
    ([`nodeport.h` L2730–2766](https://github.com/cilium/cilium/blob/v1.20.1/bpf/lib/nodeport.h#L2730-L2766)), and
    the firewall drops it (§4.3). The recorded fallback there, copying
    the public port census into machine configuration, is for the rows
    the Gateways answer the design itself. Aetf/kluster#405 corrected
    the statement and the comment; slice 1 adds the openings.
12. **`Local` on shared node addresses.** architecture.md §3.1 leaves the
    traffic policy to each Service, and architecture.md §3.3 and
    cluster-infra.md §2 give the Gateways `Local`, on the node addresses
    `internet-gw` shares with the raw Services; LB IPAM refuses to share
    an address between Services whose policies differ, or between
    `Local` Services that select different pods (§4.4). Repaired by
    slices 5 and 12.
13. **Where a cloud node resolves names.** The Talos component's
    account of the worker's link says the worker's resolvers fall back
    to Talos' defaults, "which is what the cloud nodes effectively use
    too"; Talos' `oracle` platform gives every cloud node
    `169.254.169.254`
    ([`oracle.go` L102–105](https://github.com/siderolabs/talos/blob/v1.13.9/internal/app/machined/pkg/runtime/v1alpha1/platform/oracle/oracle.go#L102-L105)),
    and a platform's resolvers outrank the defaults
    ([`configlayer.go` L7–19](https://github.com/siderolabs/talos/blob/v1.13.9/pkg/machinery/resources/network/configlayer.go#L7-L19)),
    which is why pods resolve through the host on a cloud node (§4.5).
    Repaired by slice 1.

--------------------------------------------------------------------------------

## 11. What is already conformant

*   **`kluster.lib.k8s`**: `helm_chart`, `sealed_secret` with its scopes
    and the `template.data` shape, `lb_pool_labels`, and the lookup into
    what a chart rendered. The stacks call them as written; §3.4 adds
    the manifest fetch beside them, and §9.1 adds `route`.
*   **The route census and the rewrite derivation.** `Route.zones`
    already defaults to the primary, and `dns` already writes a LAN-side
    row's rewrites at the VIP its exposure selects (§9.3).
*   **The conventions the objects are named from**: the pools' label and
    names, the Gateways' names and namespace, the ASNs, KubePrism's
    port, the pod and service ranges, the storage-class names and the
    local-path root.
*   **The CRD bindings**, which carry every kind declared here; the four
    Cilium kinds the planned generator drops are ones the agent writes
    and nothing here declares.
*   **The CI chain's order**, `physical` then `k8s-base` then `apps`, and
    the `k8s-base` and `apps` Environments, which exist.

--------------------------------------------------------------------------------

## 12. What does not propagate

*   **From architecture.md §3.3 and cluster-infra.md §2, the `Local`
    policy and Envoys pinned to nodes.** Neither describes the pinned
    implementation, and `Local` could not be allocated on the shared
    node addresses (§5.1). The client's address, which is what `Local`
    was for, survives without it.
*   **From architecture.md §3.1, a traffic policy chosen per Service on
    the node addresses.** LB IPAM holds every Service there to one
    (§4.4).
*   **From physical.md §2, every Service port answered ahead of the
    node firewall**, and its recorded fallback of copying the census into
    machine configuration. For the rows the Gateways answer, the copy is
    the design (§4.3).
*   **From cluster-infra.md §2, the deny of all of `169.254.0.0/16`.**
    It keeps its purpose and loses one address (§4.5).
*   **From cluster-infra.md §2 and architecture.md §3.2, the Egress
    Gateway enabled with the stack.** It needs BPF masquerading and, on
    this platform, the legacy-routing switch with it. The one workload
    that uses it is hath, and the wave that moves hath decides between
    it and an address of the node's own (§4.1, §15.3).
*   **From physical.md §1, workloads.md §1 and dns.md §5, listeners
    "declared beside the services"** and `public_port` emitting them.
    The partition forbids it (§5.3).
*   **From framework/pulumi.md §3.2, a pin a program and a script both
    read held in `kluster.lib`.** The script reads the `versions:` block
    from the file instead (§3.4).
*   **From the chart register's docstring, renovate reading none of it.**
    The record `update_crds` writes is what lets a bump open and finish
    by hand (§3.4).
*   **From credentials.md §3's slot map, a sealed value as a manifest
    committed beside the code.** It is a value in the declaring stack's
    configuration (§6.2).
*   **From migration.md §1, the sealing key restored with `k8s-base`.**
    Nothing in M2 needs it (§6.3).
*   **From item 6 of cluster-infra.md §1, the alert read route as part
    of the stack's first install.** It waits for its reader (§7.4).
*   **From the monitoring chart, its downloaded rules and dashboards,
    and its control-plane scrape jobs** (§7.1).
*   **From Talos' generated configuration, `baseline` as the level of
    every namespace but `kube-system`.** Each namespace of the stack
    states its own (§8).
*   **From credentials.md §3, one webhook for both producers**, and the
    DNS-01 token's scope (§6.2, §7.3).

--------------------------------------------------------------------------------

## 13. The documents this content lands in

| Document | What lands there |
| --- | --- |
| [cluster/architecture.md](../cluster/architecture.md) | architecture.md §2.2: the installation both guides document, and why host routing and masquerading take the stack's path, from this document's §4.1. architecture.md §3.1 and §3.2: every Service on the node addresses takes `Cluster`, and the reserved-address fallback's cost in client address, from §4.4; the census rows' listeners, from §5.3. architecture.md §1.1, §3.2 and §3.5: the Egress Gateway left to hath's wave, from §4.1. architecture.md §3.3: where a Gateway answers and why its client address survives, from §5.1. architecture.md §4.1: the baseline deny's exception, from §4.5, and the Gateways' ports among the node's openings, from §4.3. architecture.md §4.3: the rule contract and alertmanager's own webhook, from §7.2 and §7.3. architecture.md §5.1: listeners from the census. |
| [declarative/cluster-infra.md](../declarative/cluster-infra.md) | cluster-infra.md §0: what the stack reads, from §3.1, and each namespace's Pod Security level, from §8. cluster-infra.md §1: the chart pins in the `versions:` block and the record `update_crds` writes, from §3.4; local-path-provisioner as declared resources, from §8; the monitoring component's settings, from §7.1. cluster-infra.md §1.1: the sealed census and the stack configuration its values live in, from §6.2. cluster-infra.md §2: the recommended values and what the design adds, the Egress Gateway's deferral, the pools' inputs and the traffic-policy rule, the baseline, BGP's placement, the class configuration, listeners, certificates and their renewal, from §4 and §5. cluster-infra.md §3: a component's own routes, from §7.4. |
| [declarative/physical.md](../declarative/physical.md) | physical.md §0: the GUA output. physical.md §1: the census listeners and their source preservation, from §5.3. physical.md §2: kube-proxy, host DNS and KubeSpan's MTU, from §4.2, and the Gateway and pod-reached host ports, from §4.3. physical.md §6: the verification items slice 13 runs, which slice 2 adds, and the Egress Gateway's item moved to hath's wave, from §4.1. |
| [declarative/workloads.md](../declarative/workloads.md) | workloads.md §1: `public_port` emits the Service alone, under `Cluster`; the route label and the admission of `ingress`; the scrape-object convention in place of the `release` label. |
| [declarative/dns.md](../declarative/dns.md) | dns.md §4: the served zones and their certificates, from §5.2. dns.md §5: `route` in `kluster.lib.k8s`, from §9.1, and `public_port` without the listener. dns.md §2: the zone set of a LAN-side row, from §9.3. |
| [cluster/migration.md](../cluster/migration.md) | migration.md §1: the sealing key's restore at the first ported manifest, from §6.3, and the Egress Gateway's check out of the gate, from §4.1. migration.md §2: the monitoring move carries the dashboard's name and the read route, from §7.4. |
| [cluster/security-audit.md](../cluster/security-audit.md) | H1's fix: the baseline's exception for the host DNS address, from §4.5. M3: the Gateways' ports among what machine configuration opens, from §4.3. |
| [framework/pulumi.md](../framework/pulumi.md) | framework/pulumi.md §3.2: the rule for a pin a program and a script both read replaced by its new text — the pin lives in the `versions:` block, and the script reads `Pulumi.yaml` through the accessor's parser; the structured `value:` form; the `manifest-<name>` kind; no stack file overrides a pin a script reads; from §3.4. framework/pulumi.md §4: the record `update_crds` writes and the test holding it, from §3.4. |
| [framework/ci.md](../framework/ci.md) | framework/ci.md §5: the `no stack named` paragraph replaced, from §3.3, and the kubeconfig's red, which lasts until the copy, from §3.1. |
| [credentials.md](../credentials.md) | credentials.md §1, rule 6: the SealedSecret channel is a plain value in the declaring stack's configuration, from §6.2. credentials.md §3: the DNS-01 row's scope, the new webhook row and the existing one's consumers, the sealed slots of §6.2's values, and the zones row's `apps` target. credentials.md §4: the seal step of the commands that write them. credentials.md §3's kubeconfig row, §4's `derived sync` row and §4.1's stage 10: the kubeconfig's copy into both stacks' configuration, from §3.1. |
| [operations.md](../operations.md) | operations.md §1: the chart rows' opener. operations.md §4: the cluster's check-in and the second intake automation. operations.md §5: the playbook contract's test. operations.md §2.5: when the kubeconfig's copy is made again, from §3.1. |
| [style/pulumi.md](../style/pulumi.md) | Under "Layering", the StackReference uses and why a secret crosses a passphrase split as a copy instead, and the read that stops the run for the value a program cannot run without, from §3.1. |
| `README.md` | The `packages/crds/` row names the `versions:` block as what the bindings are rendered from. |

--------------------------------------------------------------------------------

## 14. How we get there

**Before M1's bring-up** means built and tested against Pulumi mocks,
with nothing live; such a slice can merge before the first `physical`
apply, and the chain applies it with the first push after that apply
(§3.2). **Live** means the slice needs the running cluster to finish,
for a sealed value or a measurement.

Each slice's **After** line is complete: it names the slices it depends
on and every slice it shares an owned path with, so two slices whose
lines do not reach each other can run at once — slices 1 and 3 at the
start, then 2 beside 3, 8 beside 4 to 7, and 12 beside 10 and 11. The
`k8s-base` slices form one line, because each adds its component to the
one stack program. The change that built the subnet's filter,
[Aetf/kluster#405](https://github.com/Aetf/kluster/pull/405), has
merged; it corrected physical.md §2's statement of finding 11, and slice
1 starts from its Talos component. `docs/framework/ci.md` is serialized, and
slice 3 is the one slice here that touches it. The M2 review checkpoint
follows the last slice.

**Slice 1: the census's new fields, and the machine configuration's
half of the datapath.** Before M1's bring-up. **After**
Aetf/kluster#405, which has merged.

*   **Done means**: the public port census's rows carry a name, their
    transports, their front and what answers them (§5.3); the node
    firewall opens the ports of the rows the Gateways answer from
    anywhere on every node shape (§4.3), and Cilium's four metrics ports
    and the node exporter's from the cluster's own ranges and nothing
    else; the control-plane configuration disables kube-proxy; every
    node's configuration states host DNS forwarding and KubeSpan's MTU
    from the new `conventions.KUBESPAN_MTU`; physical.md §2,
    architecture.md §4.1, the security audit's M3, and the comments and
    test comments that call kube-proxy absent, are true.
*   **Owned paths**: `src/kluster/components/talos/__init__.py`,
    `src/kluster/conventions/cluster.py`,
    `src/kluster/conventions/__init__.py`, `tests/test_talos_config.py`,
    `tests/test_conventions.py`, `docs/declarative/physical.md`,
    `docs/cluster/architecture.md`, `docs/cluster/security-audit.md`.
*   **Tests that fail without it**: every shape opens each Gateway row's
    port to both families' whole range (drop 443 from the openings;
    derive them from every row); the pod-reached openings' sources are
    exactly the kubelet's (open one to the internet; drop the pod
    ranges); kube-proxy disabled in every control-plane shape (drop the
    field); host DNS forwarding stated on every shape (drop it); the
    KubeSpan document's MTU is the convention (drop the field); the
    census's rows are unique by name, and a row the Gateways answer is a
    balancer row (a census invariant); `talosctl validate --strict` over
    every shape, which exists.
*   **Unproven live**: after the apply, no `kube-proxy` DaemonSet
    exists, the `kubespan` link carries the stated MTU, and every
    KubeSpan peer is up.

**Slice 2: the balancer's census listeners, and the node GUAs
exported.** Before M1's bring-up. **After** slice 1.

*   **Done means**: the balancer declares a listener and a backend set
    per family for every row it fronts, named after the row, with source
    preservation on, checked over TCP, and refuses a UDP-only row;
    `physical` exports the node GUAs under a new `PHYSICAL_OUTPUTS`
    field; physical.md §6 lists the verification items slice 13 runs;
    the documents of §13 for physical.md §0 and §1, workloads.md §1,
    dns.md §5, architecture.md §3.2 and §5.1, and cluster-infra.md §0
    are true.
*   **Owned paths**: `src/kluster/conventions/outputs.py`,
    `src/kluster/conventions/__init__.py`,
    `src/kluster/components/cloud/nodes.py`,
    `src/kluster/stacks/physical.py`, `tests/test_cloud_nodes.py`,
    `tests/test_physical_stack.py`, `tests/test_conventions.py`,
    `docs/declarative/physical.md`, `docs/declarative/workloads.md`,
    `docs/declarative/dns.md`, `docs/cluster/architecture.md`,
    `docs/declarative/cluster-infra.md`.
*   **Tests that fail without it**: a balancer row yields a listener and
    a backend set per family named after the row (name one after its
    port); every census backend set preserves the source (drop it on
    one); the VIP's row yields none (derive from every row); the
    listener's protocol follows the transports (declare syncthing TCP
    only); the row names and the management ports' are disjoint and fit
    the balancer's limit (a census invariant); the export set equals
    `PHYSICAL_OUTPUTS.names()`, which exists and now reads the new field.
*   **Unproven live**: the listeners exist on both families; after slice
    9, the `https` backend set is healthy on every cloud node.

**Slice 3: the two stacks exist.** Before M1's bring-up, with an
operator step. **After** nothing.

*   **Done means**: `Pulumi.k8s-base.yaml` and `Pulumi.apps.yaml` are
    committed from the operator's `pulumi stack init`, each disabling
    the default providers of the packages it builds; each entrypoint is a
    program that builds its providers (§3.1) and declares nothing else;
    the zones token's slot map writes `apps`'s configuration too, and the
    operator's mint has written it; framework/ci.md §5 and
    style/pulumi.md are true (§13).
*   **Owned paths**: `Pulumi.k8s-base.yaml` and `Pulumi.apps.yaml` (new),
    `Pulumi.dns.yaml` (the re-minted token),
    `src/kluster/stacks/k8s_base.py`, `src/kluster/stacks/apps.py`,
    `src/kluster/scripts/credentials/slots.py`,
    `tests/test_stack_programs.py` (new), `tests/test_slots.py`,
    `docs/framework/ci.md` (serialized), `docs/style/pulumi.md`,
    `docs/credentials.md`.
*   **Tests that fail without it**: under mocks, each program builds
    exactly one Kubernetes provider from `PHYSICAL_OUTPUTS.kubeconfig`
    read with `require_output` (read it with `get_output`; read another
    name); `apps` builds its Cloudflare provider from its own
    configuration (drop it); each stack file disables its defaults
    (remove the key); the zones row targets both stacks (drop `apps`).

**Slice 4: the chart set in the `versions:` block.** Before M1's
bring-up. **After** slice 2.

*   **Done means**: every chart the stack installs is a structured
    `versions:chart-<name>` pin in `Pulumi.yaml`, and the Gateway API
    definitions a `versions:manifest-<name>` pin (§3.4); `lib/versions.py`
    parses both from a mapping a program or the file provides;
    `update_crds` reads `Pulumi.yaml` through that parser, keeps only its
    tool pins and the Cilium source tree's paths in `pins.py`, checks
    each floor against the chart's own operator version, and writes the
    record of what it rendered into `packages/crds`; the manifest fetch
    is in `kluster.lib.k8s` and `helm_chart` takes a parsed pin;
    renovate's managers read the block, a manifest's release and digest
    in one match, and the dead package rule is rewritten;
    cluster-infra.md §1, framework/pulumi.md §3.2 and §4, operations.md
    §1 and the `README.md` row are true.
*   **Owned paths**: `Pulumi.yaml`, `src/kluster/lib/versions.py`,
    `src/kluster/lib/k8s.py`, `src/kluster/scripts/update_crds/`,
    `packages/crds/` (regenerated with the record), `renovate.json5`,
    `tests/test_versions.py`, `tests/test_update_crds.py`,
    `tests/test_k8s.py`, `docs/declarative/cluster-infra.md`,
    `docs/framework/pulumi.md`, `docs/operations.md`, `README.md`.
*   **Tests that fail without it**: the parser reads the same pin from
    the program's configuration and from the file (feed the file a
    different shape); a pin written as an object outside `value:` is
    refused by name; no stack file overrides a pin the script reads
    (add one); the record equals the block's rendering pins (bump one
    pin alone); the Cilium source tree's ref is the Cilium chart's
    version (hard-code a ref); a manifest whose digest differs is
    refused (change a byte of the fixture); `helm_chart` installs the
    pin's repository and version (pass a literal); each new manager
    matches its pins and nothing else, and the manifest's captures the
    release and the digest in one match (capture the release alone), in
    the renovate fixture tests, which exist.

**Slice 5: Cilium.** Before M1's bring-up; applies after it. **After**
slices 2, 3 and 4.

*   **Done means**: the Gateway API definitions from their pin, then
    the chart with §4.1's values; the `GatewayClass` and its
    configuration (§5.1); both pools (§4.4); the baseline policy (§4.5);
    cluster-infra.md §2, architecture.md §2.2, §3.1, §3.2, §3.3 and
    §4.1, physical.md §6, migration.md §1, and the security audit's H1
    are true.
*   **Owned paths**: `src/kluster/components/cilium/` (new),
    `src/kluster/stacks/k8s_base.py`, `tests/test_cilium.py` (new),
    `docs/declarative/cluster-infra.md`, `docs/cluster/architecture.md`,
    `docs/cluster/security-audit.md`, `docs/declarative/physical.md`,
    `docs/cluster/migration.md`.
*   **Tests that fail without it**: the values both guides prescribe,
    each as written (keep `SYS_MODULE`; let Cilium mount the control
    groups);
    the replacement on and pointed at KubePrism (turn it off; name port
    6443); the MTU is `KUBESPAN_MTU` (subtract 50); non-default-deny
    policies on and the Gateways' secret sync on (turn either off); no
    BPF masquerading, no legacy-routing switch and no Egress Gateway
    (set any); the class
    configuration states `Cluster`, no node ports and both families
    (drop the policy, which the CRD would default the same way, so the
    test reads the declared field); every Service the stack declares on
    the node addresses states `Cluster` (state `Local` on one); the
    `internet` pool's blocks are exactly the outputs' node addresses and
    the VIP (add a public IPv4); the deny in the baseline excepts the
    host DNS address and switches default-deny off in both directions
    (drop the exception; drop the switch).
*   **Unproven live**, in slice 13.

**Slice 6: the operators that need no secret.** Before M1's bring-up.
**After** slice 5.

*   **Done means**: sealed-secrets (§6.1), cert-manager's chart,
    local-path-provisioner as declared resources with its two image pins
    and its StorageClass, and reloader are installed in cluster-infra.md
    §1's order, each a component area; each namespace the slice creates
    states its Pod Security level, and reloader's container security
    context is set to meet `restricted` (§8).
*   **Owned paths**: `src/kluster/components/sealing/`,
    `src/kluster/components/certificates/`,
    `src/kluster/components/local_path/` (with the files its
    configuration is rendered from) and
    `src/kluster/components/reloader/` (all new), `Pulumi.yaml` (the two
    image pins), `src/kluster/stacks/k8s_base.py`,
    `tests/test_standing_set.py` (new),
    `docs/declarative/cluster-infra.md`.
*   **Tests that fail without it**: the controller's name and namespace
    are `kubeseal`'s defaults (drop the override); cert-manager's
    definitions on and its start-up check off (flip either);
    local-path's configuration names `LOCAL_PATH_ROOT` for its class
    (change it), its class is the default and reclaims `Delete` (flip
    either), and both its images come from their pins (inline one);
    cert-manager's and reloader's namespaces enforce `restricted` and
    local-path's `privileged` (swap two); reloader's container drops
    every capability and refuses privilege escalation (leave its context
    empty); each chart reads its pin (pass a literal version).

**Slice 7: CNPG, VolSync, NFD and the GPU plugin.** Before M1's
bring-up. **After** slice 6.

*   **Done means**: the CNPG operator from its pin, the barman plugin
    and VolSync, installed after cert-manager; NFD and the GPU plugin,
    the plugin inert until the worker's GPU is bound; each namespace's
    Pod Security level (§8).
*   **Owned paths**: `src/kluster/components/postgres/` and
    `src/kluster/components/gpu/` (new),
    `src/kluster/components/backup/`, `src/kluster/stacks/k8s_base.py`,
    `tests/test_operators.py` (new).
*   **Tests that fail without it**: each is installed from its pin
    (pass a literal version); the database and backup operators after
    cert-manager (drop the dependency); their namespaces enforce
    `restricted`, and NFD's and the GPU plugin's `privileged` (swap
    two).

**Slice 8: sealing in `credentials`.** Before M1's bring-up. **After**
slices 2 and 3.

*   **Done means**: `conventions.sealed`, each row naming the stack that
    declares the value; `conventions.routes` derives the served zones
    and names the route label (§5.2); `kubeseal` pinned in `mise.toml`;
    the seal step in the DNS-01 mint (scoped to the served zones), the
    BGP record and a new record row for alertmanager's webhook, each
    writing the ciphertext into the declaring stack's configuration at
    the path its row derives (§6.2); the register's rows and rule 6's
    channel (§6.2, §7.3, §10 findings 8 and 9). `mise.toml` is on the
    prose step's list of what the checker reads, so this slice's pull
    request checks every markdown file in the tree (framework/ci.md
    §3).
*   **Owned paths**: `src/kluster/conventions/sealed.py` (new),
    `src/kluster/conventions/routes.py`,
    `src/kluster/conventions/__init__.py`,
    `src/kluster/scripts/credentials/`, `mise.toml`,
    `tests/test_sealing.py` (new), `tests/test_slots.py`,
    `tests/test_conventions.py`, `docs/credentials.md`.
*   **Tests that fail without it**: a value sealed with a test key pair
    opens with `kubeseal --recovery-unseal` under its name and namespace
    and not under another (seal namespace-wide); the seal step writes
    the ciphertext as a plain value at its row's path in its row's
    stack, through the `pulumi` runner the package already fakes in its
    tests (write it under another key; write it as a secret); the served
    zones are exactly the zones the rows name (add a zone no row names);
    the DNS-01 mint scopes to them (scope to every zone); the register
    column holds every new slot (the existing seam).

**Slice 9: the issuer, the certificates and the Gateways.** Live.
**After** slices 7 and 8, with slice 6 applied and the operator's mint
having sealed the DNS-01 token into the branch.

*   **Done means**: the `ClusterIssuer`, one certificate per served
    zone, the three Gateways with their addresses, listeners and
    redirect (§5.1, §5.2); cluster-infra.md §2 and dns.md §4 are true.
*   **Owned paths**: `src/kluster/components/certificates/`,
    `src/kluster/components/cilium/`, `src/kluster/stacks/k8s_base.py`,
    `Pulumi.k8s-base.yaml` (the sealed token),
    `tests/test_front_door.py` (new),
    `docs/declarative/cluster-infra.md`, `docs/declarative/dns.md`.
*   **Tests that fail without it**: a certificate per served zone for the
    apex and the wildcard (derive from a fixed list); each Gateway's
    addresses are its pool's (swap two); the listeners' ports are the
    census rows the Gateways answer (hard-code 443); the HTTPS listeners
    reference their own zone's certificate (cross two); `allowedRoutes`
    selects the route label (allow every namespace); the issuer's token
    Secret comes from the stack's `sealedSecrets` value (inline a
    ciphertext).
*   **Live**: a certificate issued for the primary; each Gateway's
    Service holds exactly its addresses.

**Slice 10: BGP to the gateway.** Live. **After** slice 9, with the
password sealed into the branch.

*   **Done means**: §4.6's three objects and the sealed password.
*   **Owned paths**: `src/kluster/components/cilium/`,
    `src/kluster/stacks/k8s_base.py`,
    `Pulumi.k8s-base.yaml` (the sealed password), `tests/test_cilium.py`,
    `docs/declarative/cluster-infra.md`.
*   **Tests that fail without it**: the cluster configuration selects
    the worker alone (select every node); the peer carries the password
    reference and both families (drop one); the advertisement selects
    the `lan` pool label (select every Service).
*   **Live**: the session is established on both families, and the
    gateway rejects a bogus prefix (physical.md §6).

**Slice 11: monitoring.** Live. **After** slice 10, with slice 14's
webhook made and its address sealed into the branch.

*   **Done means**: §7.1's installation and settings, metrics-server
    and Grafana among them; §7.2's rules, §7.3's receiver and heartbeat;
    the monitoring namespace's level (§8); the playbook contract's test;
    the documents of §13 for architecture.md §4.3, cluster-infra.md §1
    and §3, migration.md §2 and operations.md §4 and §5.
*   **Owned paths**: `src/kluster/components/monitoring/` (new),
    `src/kluster/stacks/k8s_base.py`, `Pulumi.k8s-base.yaml` (the sealed
    webhook), `tests/test_monitoring.py` (new),
    `docs/cluster/architecture.md`, `docs/declarative/cluster-infra.md`,
    `docs/cluster/migration.md`, `docs/operations.md`.
*   **Tests that fail without it**: every declared rule carries a tier
    from the census, a summary and a playbook that resolves to a section
    (a tier outside the census; a playbook naming a missing section);
    the sync job, default rules and the three control-plane jobs are off
    (turn one on); the receiver reads the sealed Secret (inline a URL);
    the namespace enforces `privileged` with its reason (label it
    `restricted`).

**Slice 12: the application base, `route`, and the front-door check.**
Built before M1's bring-up; merges after slice 9 is applied. **After**
slices 8 and 9.

*   **Done means**: §9.1's base and `route` in `kluster.lib.k8s`; the
    echo component, its public row and its image pin (§9.2); dns.md §5,
    dns.md §2 and workloads.md §1 are true, the last of these with the
    traffic-policy rule of §4.4 and without the `release` label (§10,
    findings 10 and 12).
*   **Owned paths**: `src/kluster/components/apps/`,
    `src/kluster/lib/k8s.py`, `src/kluster/conventions/routes.py`,
    `src/kluster/stacks/apps.py`, `Pulumi.yaml` (the image pin),
    `tests/test_apps.py` (new), `tests/test_k8s.py`,
    `tests/test_conventions.py`, `docs/declarative/dns.md`,
    `docs/declarative/workloads.md`.
*   **Tests that fail without it**: `route` attaches the gateways the
    exposure selects and publishes records only for public rows (attach
    every row to `internet-gw`; publish a LAN-only row's record); the
    namespace enforces `restricted`, carries the route label and admits
    `ingress` (drop any); the census invariants of rfc-003 §6.6 hold
    with the new row, which exists.
*   **Live**: the exit criterion — the name resolves to the balancer,
    and a request answers through it with the client's address in the
    echoed headers.

**Slice 13: the M2 bootstrap gate.** Live; no pull request unless an
item fails. **After** slices 10, 11 and 12.

*   **Done means**: migration.md §1's verification gate run for the
    items that need Cilium, and the items slice 2 adds to physical.md
    §6: no Gateway Service carries a node port or a health check port;
    443 answers through the balancer, and an undeclared port at a node's
    public address does not; Envoy's log shows the client's address
    through the balancer's IPv4 front; a request answers through its
    IPv6 front, whose log shows the client's address where physical.md
    §6's IPv6 item kept source preservation and the balancer's address
    where its fallback was taken, that fallback's accepted cost (§5.1);
    the cluster DNS resolves through host DNS with the baseline on, and a
    pod's request to `169.254.169.254` is refused (§4.5); a renewal
    cert-manager is made to perform is served by every Gateway with no
    Envoy restarted (§5.2); vmagent scrapes the node exporter on a node
    other than its own (§4.3); an
    alert raised through alertmanager reaches the phone, and stopping
    vmalert pushes the missed check-in (§7.3). A transcript per item on
    the ops issue; an item that fails is its own issue, and a fallback it
    flips lands in the document that records it.

**Slice 14: Home Assistant's side.** Operator, in Home Assistant; no pull
request. **After** nothing; slice 11 waits for it.

*   **Done means**: the alertmanager intake automation and the cluster's
    check-in timer exist, built against the contract §7.3 states; the
    webhook's address is handed to slice 8's record row.

The slice that lands the last piece flips this document's status to
Implemented and fills in where its content lives.

--------------------------------------------------------------------------------

## 15. Open questions

### 15.1 Ruled: M1's first `physical` apply waits for slice 1

**(a), ruled by the operator.** The first `physical` apply carries slice
1, so the cluster never runs kube-proxy and the Gateways' ports are open
from the first boot. Slice 2 is dispatched when slice 1 merges and rides
the same apply if it is ready by then; the bring-up does not wait for
it. What the ruling avoids, from the pinned releases:

*   machined applies a bootstrap manifest only when its object is
    missing, and never deletes one, so turning the proxy off later
    would leave its DaemonSet running
    ([`manifest_apply.go` L335–350](https://github.com/siderolabs/talos/blob/v1.13.9/internal/app/machined/pkg/controllers/k8s/manifest_apply.go#L335-L350));
*   `talosctl upgrade-k8s` prunes what is no longer generated unless it
    runs with `--manifests-no-prune`
    ([`upgrade-k8s.go` L64](https://github.com/siderolabs/talos/blob/v1.13.9/cmd/talosctl/cmd/talos/upgrade-k8s.go#L64),
    [`talos_managed.go` L553–561](https://github.com/siderolabs/talos/blob/v1.13.9/pkg/cluster/kubernetes/talos_managed.go#L553-L561));
*   kube-proxy removes the rules it programmed only when run with
    `--cleanup`
    ([`options.go` L105](https://github.com/kubernetes/kubernetes/blob/v1.36.3/cmd/kube-proxy/app/options.go#L105)),
    so each node would keep them until it rebooted.

### 15.2 Settled on first contact

*   The monitoring database's retention and volume size, from the legacy
    numbers.
*   alertmanager's routing tree beyond the tiers (declarative/README.md
    §4 leaves it to the port from the legacy configuration).
*   The echo server's image.
*   The gateway's IPv6 neighbor address for the BGP session, read off
    the device's routing configuration the `physical` stack writes.
*   Whether a pool that holds node addresses works (physical.md §6), and
    the reserved-address fallback, at the cost §4.4 states, if it does
    not.

### 15.3 Left to the migration's design

*   **Which name the alert read route answers on**, and whether
    `k8s-base` publishes a record for it — which would put the zones
    token in its configuration — or the route rides a name another stack
    publishes (§7.4).
*   **The legacy sealing key's channel** in the credential register
    (§6.3).
*   **How hath's traffic leaves on its own address** (architecture.md
    §3.2), in hath's wave. Two options:
    *   **(a) Recommended: the reserved public address on the primary
        private address of the node hath runs on**, in place of a
        secondary address. OCI assigns a reserved public address to a
        primary or a secondary private address, and moves it to another
        private address at any time
        ([OCI: Public IP addresses](https://docs.oracle.com/en-us/iaas/Content/Network/Tasks/managingpublicIPs.htm)).
        Every pod on that node then leaves through the reserved address
        by ordinary masquerading, and hath's Service is on that node's
        own address, already an `internet` pool member. No Cilium
        feature is involved. Its costs: hath is held to that node,
        which its block volume does already; the node's own public
        address is the reserved one; and the node's other pods share
        hath's egress address.
    *   **(b) The Egress Gateway, as architecture.md §3.2 designs it.**
        It brings BPF masquerading, and on this platform the
        legacy-routing switch with it, to every node for one workload
        (§4.1).
*   **Whether the state-backend appliance's pins move into the
    `versions:` block** under framework/pulumi.md §3.2's new text
    (§3.4). They are read by the `state-backend` script and the
    appliance's program alike, the case the old text was written for;
    moving them is that appliance's own change.
