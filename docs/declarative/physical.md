# Declarative Design: the `physical` Stack

How the physical layer is declared — providers, resource graph, and
bootstrap order for everything that must exist before the k8s API does.
The *why* of the architecture lives in
[cluster/architecture.md](../cluster/architecture.md); this document is
the *how* for the `physical` stack of [README.md](README.md) §1.

> **Status**: designed 2026-08-22; provider choices verified against
> current releases (pulumiverse-talos 0.8.1 wrapping the official
> siderolabs terraform-provider 0.11). **Declared in full, applied
> nowhere.** `src/kluster/stacks/physical.py` calls every area this
> document describes, and each one is written: the OCI network, image,
> load balancer and nodes (§1) and the Talos day-1 chain (§2) in the
> `cloud` and `talos` areas of `src/kluster/components/`, the libvirt
> worker VM (§3) in `components/homelab/`, the UDM's container services
> and firewall (§4) in `components/gateway/` with the overlay
> configuration beside it in `components/overlay/`, and the B2
> bucket (§5) in `components/backup/` — so a run stops at no named gap.
> Nothing described here has been provisioned: the stack has never been
> applied, so §6's bootstrap gate is entirely ahead of it.

## 0. Scope and outputs

Owns: OCI (network, nodes, NLB, IPs, volumes, guardrails),
libvirt on the homelab host (the worker VM and its storage), Talos
day-1 (secrets → configs → apply → bootstrap), the device files and
unifi resources on the UDM, and the B2 backup bucket. DNS lives in
the `dns` stack (declarative/dns.md), which consumes this stack's IP
outputs.

Explicitly **not** owned: the state-backend E2.1.Micro (the backend
this stack's state lives in — declared by the `state-backend` stack,
whose state is committed, documented in
[framework/ci.md](../framework/ci.md) §1) and
anything speaking the k8s API (that's `k8s-base`/`apps`).

Stack outputs (the machine facts other stacks may reference,
[README.md](README.md) §2) are named by `conventions.PHYSICAL_OUTPUTS`
(`conventions/outputs.py`), which is the list and which a test holds
the stack's exports to: `kubeconfig`, `talosconfig`, per-node
public/private IPs, both of the NLB's public addresses (IPv4 and IPv6 —
the cluster anchor in `dns` carries an A and an AAAA), the
dedicated-VIP addresses (reserved public + secondary private), the
backup bucket's name and S3 endpoint, the backup keys (one application
key per scope, `backup_keys`), and each CI overlay member's join
credential (`ci_zerotier_identity_physical`,
`ci_zerotier_identity_dns`).

## 1. OCI (pulumi-oci)

-   **VCN**: dual-stack (IPv4 + the assigned /56 GUA), one public
    subnet, internet gateway, and a **service gateway** (2026-08-24 —
    node ↔ Object Storage/OCIR traffic rides the in-region $0 path
    instead of the IGW). **The subnet admits everything**: it carries
    one security list of this stack's own, which admits every protocol
    from `0.0.0.0/0` and `::/0` and lets every protocol out to both, all
    four rules stateless. A subnet naming no list would carry the VCN's
    default one, whose rules are OCI's choice rather than this
    program's, so the list is declared and is the subnet's only one.
    Stateless because a rule that admits every packet both ways has
    nothing to decide by a connection's state, while tracking it costs a
    table on each VNIC that drops new connections when it fills. The
    list applies to the NLB's VNIC too, which answers only on its
    listeners. **The filter is the node's**: the Talos ingress firewall
    decides every port in the host network namespace (§2), and what
    Cilium's datapath answers ahead of it is the declared raw TCP/UDP
    Services, while no Service allocates a `NodePort`
    (cluster-infra.md §2).
-   **Image**: no official Talos OCI image — an `image_factory_schematic`
    (talos provider) pins the schematic (platform `oracle`, arm64, no
    extensions initially), and a custom-image import brings the
    factory-built image into OCI. The schematic ID is part of the
    declared state, so image contents are reproducible.
-   **Nodes**: 3× `VM.Standard.A1.Flex` (1 OCPU / 8 GB), one per
    availability domain (fault domains only as the tiebreak, nodes.md
    §5), boot volume ~50 GB. Machine config is delivered as
    base64 `user_data` in instance metadata (the Talos `oracle` platform
    reads the OCI metadata service) — day-0 needs no network apply.
    **Legacy IMDS (v1) is disabled** on every instance; note OCI's v2
    auth header is a static string, so the control that actually keeps
    pods away from `user_data` (= the machine config, secrets included)
    is the baseline network policy (architecture.md §4.1,
    cluster-infra.md §2).
-   **Two per-node capabilities, declared apart.** The **dedicated
    VIP** — a **secondary private IP** on a VNIC plus the **reserved
    public IP** assigned to it (architecture.md §3.2) — belongs to
    exactly one node, named by `conventions.DEDICATED_VIP_NODE`.
    **Block volumes** are a list any node may draw from:
    `conventions.NODE_VOLUMES` gives each one a size and the node it
    attaches to, and its name is its mount — the node mounts it at
    `/var/mnt/<name>` (§2). One volume per node, so that the machine
    configuration's disk selection stays "the disk that is not the boot
    disk" and one node's loss takes one dataset rather than two. A
    volume whose workload must also hold the VIP says so in its own
    entry (`node=FOLLOWS_DEDICATED_VIP`) and resolves to whichever node
    that is, so the pair cannot drift apart. Nothing about either node
    is workload-specific: a workload reaches the address or the volume
    through scheduling constraints declared with it.
-   **NLB**: one Network Load Balancer with source-IP preservation
    (verification item), **dual-stack**: it holds a public IPv4 and a
    public IPv6, and serves every port it forwards on both. OCI's
    listeners and backend sets are single-family — each carries one
    `ip_version`, and a listener forwards only to a backend set of its
    own family — so each management port
    (`conventions.ManagementPorts`) has a listener and a backend set
    per family, each holding every cloud node: named by the port's
    field for IPv4 (`kubernetes`) and by the field and the family for
    IPv6 (`kubernetes-ipv6`). An IPv4 backend names the instance; an
    IPv6 backend names the node's GUA, because an instance OCID stands
    for the primary private IPv4. Declared here; **listeners are not a
    fixed list** — the management listeners (6443/50000) live here,
    while service listeners are declared beside the services that need
    them, like DNS records, and a service listener is one per family as
    well.
-   **Buckets**: none on this provider. The installation's
    cluster-data bucket is the backup bucket, which lives on B2
    precisely because it must not share a provider with what it
    insures; the state backend's buckets are the `state-backend`
    stack's, not this stack's (storage.md §4).
-   **Protection**: data- and identity-bearing resources here — block
    volumes and their attachments, the reserved public IP,
    `machine_secrets` — carry `protect=True` per storage.md §3.3.
-   **Guardrails**: compartment quotas pinning creatable shapes to the
    free envelope, plus a budget with alert rules (nodes.md §3.2).

## 2. Talos day-1 (pulumiverse-talos)

The provider chain, one resource each:

```
machine_secrets
  → machine_configuration (data source; per-node config_patches)
    → user_data on the OCI instances / nocloud seed for the libvirt VM
    → machine_configuration_apply (subsequent config changes, over :50000)
  → machine_bootstrap (once, first CP node)
  → cluster_kubeconfig, client_configuration  → stack outputs
  → cluster_health (gate before dependents read the kubeconfig)
```

-   **Patches are Python.** Per-node machine config is assembled from
    typed Python dicts (our framework's home turf), covering: KubeSpan
    on, at the link MTU `conventions.KUBESPAN_MTU`, which rfc-007 §4.1
    has `k8s-base` size Cilium from; KubePrism; host DNS with the cluster DNS
    forwarded to it, which Talos' generated configuration already turns
    on and which is stated because the baseline network policy is shaped
    around the address it forwards to (rfc-007 §4.5); dual-stack
    pod/service CIDRs **IPv4 first** (architecture.md §1.3); on the
    control planes, no CNI and **no kube-proxy** — Cilium, which
    `k8s-base` installs, is both, and kube-proxy is off from the first
    boot because turning it off later is a procedure rather than a
    configuration change: Talos applies a bootstrap manifest only while
    its object is missing and never deletes one, so the DaemonSet stays
    until a `talosctl upgrade-k8s` prunes it, and the rules kube-proxy
    programmed stay on each node until it reboots (rfc-007 §4.2, §15.1); CP scheduling enabled
    (`allowSchedulingOnControlPlanes` — the combined CP+ingress role);
    cert SANs including the NLB IP; **etcd encryption at rest**
    (secretbox — the architecture.md §6.5 residual-risk mitigation for
    cluster secrets in a $0-trust tenancy); kubelet system-reserved so
    eviction actually works (the legacy CP-starvation lesson,
    architecture.md §6.5); the **Talos ingress firewall** (the next
    item); kube-apiserver `anonymous-auth=false` pinned and audit
    logging on (a public 6443 warrants both, defaults notwithstanding);
    the dedicated-VIP node's secondary private IP on its physical link;
    the **local-path volume** (`/var/mnt/storage`, storage.md §2 — the
    StorageClass's provisioner is k8s-base's, but the directory under
    it is machine config); and on a node a block volume
    attaches to (§1), the **node volume** and a **node label** naming
    it (`conventions.NODE_VOLUME_LABEL`), in the configuration the node
    boots with.
-   **The Talos ingress firewall is the only filter.** It is a
    `NetworkDefaultActionConfig` of `block` plus a `NetworkRuleConfig`
    per opening, and the subnet in front of it admits everything (§1;
    architecture.md §4.1). Its enumeration rule (2026-08-24) is **only
    ports that terminate in the host netns**, each opening stating its
    protocol and its sources. apid 50000/tcp and kube-apiserver
    6443/tcp (a hostNetwork static pod, so host-side despite also being
    an NLB listener) are open to anywhere. So is KubeSpan
    51820/**udp**: one of its peers, the homelab worker, comes from a
    home address that is dynamic, and WireGuard answers no packet not
    keyed to a peer. So are the ports of the public port census's rows
    the Gateways answer, on every transport the row names — 80/tcp and
    443/tcp today — on every node (the Service ports item below). The
    kubelet 10250/tcp is open to the cluster's own ranges alone (the VCN,
    the cluster VLAN and both pod ranges), and so is every other host
    port a pod calls (the next item). The
    DHCPv6 client 546/udp is open to link-local sources alone: a cloud
    node's IPv6 address is leased over DHCPv6, and the server's Reply
    comes from its own address rather than the multicast group the
    Solicit went to, so it matches no connection-tracking entry. The
    homelab worker additionally takes BGP 179/tcp from the UDM. The
    default-action document is on every node because without it Talos
    keeps the openings and accepts every other port.
-   **Only node-to-node traffic rides KubeSpan.** etcd, `trustd` and
    the apiserver's calls to a remote kubelet go between node
    addresses, which KubeSpan routes into the `kubespan` interface, and
    the ingress chain accepts that interface ahead of every rule, so no
    opening names them. Traffic from a pod to a listener in the host
    netns rides it only when the listener is on another node: Cilium's
    tunnel carries traffic addressed to pods, not to a node's own
    addresses, so that call leaves its node masqueraded to a node
    address. On the pod's own node the call arrives on the pod's device
    with a pod-range source, which no interface rule accepts. So **a host
    port a pod calls is opened from the cluster's own ranges and from
    nothing else**, the kubelet's sources applied to
    every such port. Those ports are the metrics endpoints the scraper, a
    pod, reads off processes on the host network: Cilium's agent 9962,
    operator 9963 and Envoy 9964, Hubble's metrics server 9965, and the
    node exporter's 9100 (rfc-007 §4.3). Every other port Cilium listens
    on takes no opening: node-to-node traffic rides KubeSpan, the local
    ones bind `127.0.0.1` or `::1`, and the rest serve what this
    installation does not use: the Hubble server, whose one client,
    Relay, is off, and mutual authentication, Cilium's WireGuard and
    `geneve`, which are off.
-   **Service ports.** A raw TCP/UDP LoadBalancer Service's traffic —
    NLB health checks on backend ports included — is intercepted by
    Cilium's BPF datapath at tc ingress *before* nftables and sent to a
    backend, so it serves with no firewall entry, while an undeclared
    port falls through to the host stack and hits the default-deny. For
    those Services per-service admission control *is* the KPR datapath,
    and machine config carries none of their ports. **A Gateway
    listener is different**: the datapath marks its packets for the
    node's Envoy and hands them up the host stack, where they cross the
    ingress chain like any traffic to the host netns, and only an
    opening admits them. **The Gateways' openings are derived from the
    public port census** (`conventions.PUBLIC_PORT_CENSUS`), from its
    rows the Gateways answer, and never listed: each such row is opened
    from anywhere, on every transport it names, on every node. On a
    cloud node those ports are the internet's by design, and nothing on
    the host binds them but through the proxy; on the worker, the site
    gateway forwards none of them (physical/gateway.md §4.2). The
    raw-Service half is verified both ways at bootstrap (§6); recorded
    fallback if BPF precedence fails on the chosen datapath mode: copy
    the census's other rows — the ones a Service's pods answer, rarely
    changing — into machine config the same way, accepting the
    cross-stack cost only in that world.
-   **Document kinds.** A patch is either a strategic merge into the
    `v1alpha1` document or a configuration document of its own kind,
    which the provider appends beside it. Whatever the pinned Talos
    release deprecates in `v1alpha1` travels as its own document
    instead; at v1.13 that is every field of `machine.network`. The
    documents the component emits:
    -   `KubeSpanConfig`, on every node: KubeSpan on at the link MTU
        `conventions.KUBESPAN_MTU`, every other setting Talos' default.
    -   `LinkAliasConfig`, naming the node's one physical link
        `uplink`, and a `LinkConfig` on that alias — on the two nodes
        whose machine configuration states an address. The
        dedicated-VIP node's carries the secondary private IP as a /32,
        beside a `DHCPv4Config` that keeps the node's lease, since
        configuring a link switches off Talos' default DHCP. The homelab
        worker's carries its static address with the VLAN's prefix and
        a default route via the gateway, and no lease.
    -   `NetworkDefaultActionConfig` and `NetworkRuleConfig`, the
        ingress firewall above.
    -   `UserVolumeConfig`, on each node a block volume attaches to,
        named for the volume's row: a `partition` volume selecting the
        disk that is not the boot disk (`!system_disk`), `maxSize: 100%`
        with `grow` on, XFS stated, no encryption. Talos mounts it at
        `/var/mnt/<name>` and finds it on every later boot by the
        partition label `u-<name>` it wrote; what it will and will not
        format, and the rule a PersistentVolume over it follows, are
        storage.md §6. The volume is part of the configuration the node
        boots with because the table is known at day 0, while the
        attachment waits on the instance: the disk arrives on a node
        that already runs the document. `TalosCluster` refuses a volume
        on a node outside the cluster and two volumes on one node, since
        the selector cannot tell two data disks apart.
    -   `UserVolumeConfig` named `storage` (`conventions.LOCAL_PATH_VOLUME`),
        on every node: the directory `local-path` hands out. A
        `directory` volume, which has no disk: Talos creates
        `/var/mnt/storage` on the system disk's EPHEMERAL partition and
        bind-mounts it onto itself, and a directory already at that path
        keeps its contents. The document states its type and nothing
        else, because Talos refuses a provisioning, filesystem,
        encryption or mount block for that type. It is not a node volume
        and does not count against one per node, which exists for the
        disk selector's sake; it does share Talos' one namespace of
        user-volume names with them, so no row of
        `conventions.NODE_VOLUMES` may be named `storage`.

    **A host path a pod reaches is a Talos user volume under
    `/var/mnt`, and the machine configuration carries no kubelet
    mount.** The kubelet runs in a container that gets `/var/mnt` bound
    read-only with slave propagation, so it can create nothing inside a
    plain directory there, while a mount made under `/var/mnt` reaches
    it writable. A user volume is such a mount, and it is what Talos
    names in place of `machine.kubelet.extraMounts`. The kubelet mount
    has no future besides: v1.14 deprecates it, the multi-document
    `KubeletConfig` has no mounts field, and a configuration carrying
    that document refuses any `machine.kubelet` beside it.
    `tests/test_talos_config.py` holds the rule for every node shape.

    Everything else stays in `v1alpha1`, because v1.13 has no document
    for it: the node label as `machine.nodeLabels` and the kubelet's
    reservations as `machine.kubelet.extraConfig` among it. v1.14
    deprecates much of that for documents of their own — the two named
    for a `KubeNodeConfig` and a `KubeletConfig` — and a base generated
    for its contract already carries those documents, so the move comes
    with the configuration contract rather than after it. It waits on a
    pulumiverse-talos release built on v1.14's machinery, which the
    pinned one is not (Aetf/kluster-ops#475).
-   **Talos validates what the component renders, in `checks`.**
    `tests/test_talos_validate.py` renders every node shape the
    component produces the way the provider does — the base
    configuration `talosctl gen config` generates for the pinned
    release, with the component's patches applied — and runs
    `talosctl validate --strict` over each in `cloud` mode, which is
    the mode both the `oracle` and the `nocloud` platform run in. The
    `talosctl` is `mise.toml`'s, pinned to the release `versions:talos`
    names and moved with it by one renovate rule, so a document that
    the fleet's release does not validate fails the pull request
    rather than the first `up`. The gate is stricter than a node on
    warnings, which a node returns from an apply while accepting the
    document, and it does not see what a node checks against its own
    running state on top of the document — at v1.13, the install disk
    in `metal` mode and the kubelet and control-plane image tags.
-   **Two renderings, one configuration.** What a machine boots with is
    delivered before that machine exists (`user_data`, seed image), so
    it cannot name anything the cloud assigns to the finished instance:
    a configuration naming the dedicated-VIP node's secondary private IP
    would wait on the instance that waits on it. That address arrives on
    day 1 instead — the configuration applied over apid is the booted
    one plus the address — and every other node is applied the very
    configuration it booted. This is also why the chain is two
    components: day 0 is knowable up front, day 1 needs the address each
    machine's apid listens on.
-   **Reboot-requiring config changes**: `apply_mode:
    staged_if_needing_reboot`, and CI applies node-serially, so the
    quorum never reboots together.
-   **Where a machine is reached**: apid routes by the node a call
    names, not by the connection it arrives on, so the two halves of an
    apply are separate answers. The cloud nodes are dialed at their own
    public addresses, because a call with no cluster to route through —
    the bootstrap is exactly that — has nothing else to use, and a
    balancer would pick whichever backend it liked. The worker is
    *named* by its cluster-VLAN address and *dialed* at the cluster
    endpoint, which the NLB forwards on 50000: whichever control plane
    answers proxies the call the rest of the way over KubeSpan, so the
    backend chosen does not matter and nothing outside the site needs a
    route to the worker.
-   **Footgun on record**: destroy-time `reset = true` wipes *all* disk
    partitions (provider issue #205) — never enabled on nodes carrying
    data; node replacement is explicit (drain, etcd leave, destroy,
    recreate).
-   **Secrets in state, accepted**: the TF provider's ephemeral
    resources don't bridge to Pulumi, so `machine_secrets` (cluster PKI)
    lives in Pulumi state — passphrase-encrypted, in the TLS-guarded
    Postgres backend (ci.md §1).

**Day-2 is talosctl, deliberately.** OS upgrades (`talosctl upgrade`),
`upgrade-k8s`, and etcd snapshots are imperative operations — not
wrapped in fake-declarative command resources. mise only *provides* the
tools; the procedures themselves are `just` recipes or, where real
logic is involved, Python console scripts in this repo (the
`update_crds` pattern). The
official provider's v0.12 `talos_machine`/`talos_cluster` resources add
real drift detection and upgrade orchestration; **tracking item**: adopt
them for day-2 once v0.12 is stable *and* has reached the Pulumi bridge.

## 3. Homelab (pulumi-libvirt)

-   **Worker VM**: 12–16 vCPU / 20 GiB end-state (nodes.md §4.2),
    bridged onto the cluster VLAN (id 7, `192.168.70.0/24`) at the
    static `192.168.70.10`, disk on NVMe — both disk *and* RAM start
    smaller during migration and grow per wave (~60 GB / ~10 GiB at
    bootstrap; migration.md §0.4). The VM's **system design** — disk shape (raw sparse on a
    nodatacow subvolume, virtio-blk), the second host bridge over the
    tagged VLAN interface with the host's own leg beside it, the
    two-phase GPU passthrough, and the host-prep aconfmgr change-set
    the program assumes — is
    **[physical/homelab-host.md](../physical/homelab-host.md)**; this
    section owns only how it is declared. Talos via the `nocloud`
    image variant, machine config on a cloud-init seed ISO — the seed
    carries the machine secrets: root-only permissions, and it lives
    outside every host snapshot/backup scope (the same subvolume
    discipline as the disk image).
-   **Volume**: created *from* the decompressed `nocloud` image, which
    the provider uploads into the pool over the same connection it
    defines the domains through — so the first boot follows from an
    apply rather than from an operator writing an image by hand. The
    declaration states no disk size: the provider refuses `size` beside
    `source` and takes the volume's capacity from the image, which
    makes every size the disk ever has — the bootstrap one included —
    the host-side `truncate` + `virsh blockresize` of
    homelab-host.md §1. Both `size` and `source` are then ignored on
    that resource, because a libvirt volume has no update path at all
    and any field that diverged — a file the host has grown, an image a
    Talos self-upgrade has replaced — would otherwise propose
    destroying the worker's disk. The **pool** pointing at the
    nodatacow subvolume is declared here too; the subvolume itself is
    host preparation (homelab-host.md §4).
-   **GPU hostdev**: not present at bootstrap (the two-phase plan,
    homelab-host.md §3); when the Wave C cutover adds it,
    pulumi-libvirt's hostdev support is thin — the provider's XSLT
    escape hatch may be needed for the PCI device XML (the
    home-automation domain on the same host proves the libvirt side
    works).
-   **The session**: `qemu+ssh` as the host's own service user, and the
    provider is built inside the component that owns the connection,
    which is where its private key is read and the only place that key
    appears. The URI is derived on every run rather than configured:
    the identity and the pinned `known_hosts` are written into the
    checkout and named **relative to it**, since the URI is a provider
    input in state and an absolute path is a diff no other machine can
    resolve. The transport's parameters, the pin and the working-file
    boundary they sit behind are
    [physical/homelab-host.md](../physical/homelab-host.md) §6.
-   **The home-automation domain is not declared here.** It shares the
    host, and this stack says nothing about it: no resource, no
    configuration key, no ignore list. Its full XML stays with the
    host's own configuration management, beside the rest of that host's
    preparation, and that is where recreation (a host-side `virsh
    define`) and drift detection (a normalized `virsh dumpxml`
    comparison, an operational drill) both live. Four ways of declaring
    it were measured against the live definition and the libvirt
    provider, and each is blind or destructive somewhere that matters;
    the comparison and the reasoning behind leaving it out are
    [rfc-002](../rfc/rfc-002-src-layout-and-the-gateway.md)
    §13. The libvirt session above is unaffected — it is the worker
    VM's, and so is everything §3 declares.

## 4. UDM (device-files dynamic provider + bridged filipowm/unifi)

Per architecture.md §5.2 (full push-direction absorption): the
device-files provider (SSH, `/data`, idempotent diff/apply, post-apply
hooks; the UDM's **SSH host key is pinned** — the session crosses
ZeroTier, and an accept-new first contact would hand a MITM root on
the gateway. The pin is a `conventions` constant and a declared input
on every device resource, like the address dialed: a public key that
a preview shows is a pin a reviewer can check, where a secret-typed
configuration value would be redacted in the one place anybody would
look. The client credential that answers it goes the other way — read
by the provider itself, out of stack configuration, in its own
process, and appearing on no resource: what a resource carries is a
short digest of it, which is what makes a rotation visible in a
preview) manages the device's
entire desired state — FRR/BGP
(neighbor = `conventions.HOMELAB_NODE_IPV4`, a constant and deliberately not an
output of the libvirt resource: the worker's address is written statically into
its machine config, and everything that names it — the routing session, the
peer-port forward, day 1's apid dial — reads that same constant, so no session
depends on a lease), the nspawn container services
(units + digest-pinned rootfs images from homelab-containers CI via
`DeviceArtifact` (architecture.md §5.2) — including
the **ZeroTier member container**, host-networking + `/dev/net/tun` +
`/data`-persisted identity, architecture.md §5.3), the recovery script
in `on_boot.d` that re-establishes all of it after a firmware update
and decides which services a deployment restarts (physical/gateway.md
§1.1), caddy, AdGuard static configs, secrets. What a dynamic provider
is mechanically — the pickle in each resource's state, the credential
read in `configure`, the two properties `check` injects to make a
rotation visible, and the traps that come with all three — is
[framework/pulumi.md](../framework/pulumi.md) §5. ZeroTier Central's network config
(managed routes via the UDM member, member authorizations) is managed
from the `physical` stack via the bridged `zerotier/zerotier`
provider (architecture.md §5.3). The gw-config repo retires;
periodic backup *pulls* move to a yadm timer on the homelab host.

A root filesystem is a tree rather than bytes in state, so
`DeviceArtifact`'s surface is `root` — the directory the tree is
unpacked into — plus the pin that fills it. **The device does the
pulling.** Handed `repository@digest` over the session already open, it
runs `skopeo copy` into a staging OCI layout beside the tree and
`umoci raw unpack` out of that layout, then swaps the result into place
with two renames: no archive crosses the runner and no image is decoded
on it, digest verification belonging to `skopeo` and the whiteout and
extended-attribute semantics to `umoci`. Both binaries reach the device
in the persistence layer's package set (physical/gateway.md §1.2), and
a device without them fails the pull by name. Beside the tree the device
keeps one marker naming the manifest digest it came from, which is what
a preview compares against the pin. The marker is written **before** the
hook, because the hook restarts the container only when it sees the
marker change, and a hook that does not succeed withdraws it again —
leaving a tree of unknown provenance, which the next preview reports as
work to do.

The **unifi provider (filipowm/unifi via the Terraform bridge,
architecture.md §5.1)** manages the controller-side resources, **all
in this stack** — the co-location exception on record: gateway
resources follow the gateway's credential tier, so the `apps` CI
environment never holds a controller credential; app components keep
a pointer (workloads.md §4). The census: the **cluster VLAN network
object** (id 7, `192.168.70.0/24`) and the **firewall zone of its
own** it is placed in — the isolation the shared server LAN could not
offer (physical/gateway.md §4.2) — the IoT→lan-pool zone
policy with its address groups (architecture.md §3.4 — the v4 CIDR
group and the ULA group are separate objects, UniFi address groups
being single-family), the **three zone-matrix policies the new zone
needs to be usable** (cluster→External, cluster→Internal, and
Internal→cluster with the IoT VLAN dropped ahead of it as a
single-family pair), a **`FirewallZonePolicyOrder` on every zone pair
that carries a policy** — position is not a property of a policy, and
on the inbound pair a drop declared after the allow matches nothing —
the qbittorrent v6 pinhole — declared only once `workerGua` carries
the worker's SLAAC address, which no apply before the worker's first
boot can supply (physical/gateway.md §4.2) — and its v4 peer-port
forward to `192.168.70.10` (**the only port forward**; no management
inbound exists), declared only once `qbittorrentOnWorker` says
qbittorrent runs on the worker, and any static LAN host entries (dns.md §4). The
**`lan` pool is not in this census as an object at all**: it is
deliberately no network object and therefore in no zone, so the rules
naming it are address-group rules on the internal→external pair, not
zone policies about a pool zone. Auth: a dedicated local admin with an
**API key** — never the SSH credential — and failure retries are
throttled: the UniFi global login rate-limit is not per-IP and has
locked out real users before (the HA-integration incident).

## 5. B2 (bridged provider)

The backup bucket, keys, and lifecycle rules (storage.md §4-5). (DNS —
zones, base records, anchors — moved to the `dns` stack,
declarative/dns.md.)

## 6. Bootstrap order & verification checklist

Order within the first `pulumi up`: OCI network → instances (user_data
configs) ∥ libvirt VM → bootstrap (first CP) → health → outputs; the
NLB and the device's FRR settle in parallel once IPs exist, and the `dns`
stack's anchors follow from the IP outputs.
Manual preconditions: OCI tenancy on PAYG, the state-backend micro
(ci.md §1), and the homelab host-prep change-set (§3). ZeroTier
Central config (managed routes via the UDM member, CI member
pre-auth, the flow rules, and the managed DNS — physical/gateway.md
§2) is
Pulumi-managed via the bridged zerotier provider (architecture.md
§5.3); only if that bridge proves unusable do those settings fall
back to hand-kept manual preconditions.

Bootstrap-time verifications, the gate itself (several items exercise
Cilium and therefore run only after `k8s-base` is up — the gate's place
in the sequence is
migration.md §1): LB IPAM pool containing node primary IPs; NLB dual-stack listeners +
source-preservation semantics; etcd fsync latency on OCI block volumes;
A1 capacity at creation; Egress Gateway under the chosen routing mode +
reserved-IP↔secondary-private-IP NAT; Cilium MTU over the KubeSpan
underlay; talosctl reaching the homelab node via cloud endpoints (apid
proxy); VFIO iGPU passthrough capability on a scratch VM.

The node volumes (§1, §2) are first exercised at bring-up too, and the
same gate confirms them:

-   Talos provisions the disk when OCI attaches it to a node that is
    already running, without a reboot.
-   A user volume still waiting for its disk holds up neither the boot
    nor the health gate.
-   A sentinel file written to the empty volume survives a detach, a
    re-attach and a reboot — the rebuild path, drilled while the volume
    holds nothing.
-   A pod writes through a `local` PersistentVolume at
    `/var/mnt/<name>/<dir>`, despite the kubelet's read-only view of
    `/var/mnt`.
-   With the volume unmounted, that pod stays pending (storage.md §6).
-   The node label appears on the Node object.

The local-path volume (§2) is first exercised there as well:

-   `talosctl get volumestatus u-storage` reports it ready on every
    node.
-   Once `k8s-base` runs the provisioner, a `local-path` claim binds
    and a pod writes through it.

One part of the NLB item is answered earlier, by the first
`pulumi up` that creates the balancer's IPv6 backends: each names a
node's GUA inside a source-preserving backend set, which OCI's console
guide refuses ("Preserve source IP must be disabled in the backend set
to add an IP address-based backend server") and Oracle's cloud
controller manager declares. A refusal fails that `up` at those
backends, which nothing depends on. The fallback is
`is_preserve_source` off on the IPv6 backend sets alone, and its cost
is that the apiserver's audit log records the balancer's address for
every request through the IPv6 front.

The zone-policy verification comes **before** the first `pulumi up`
rather than in the gate, because that run's first half — the targeted
apply of the gateway cutover — is what exercises it, and it runs with
the LAN's resolvers down and the machines' state already moved: the
bridged filipowm/unifi provider round-tripping a scratch
`firewall_zone_policy` (create → clean diff → delete) against the
UDM's current Network release — the resource is experimental and
targets UniFi OS ≥9, and a failure here flips the rules to the
device-files provider's `UnifiFirewallPolicy` fallback
(architecture.md §5.1). The same probe checks the legacy port-forward
endpoint still accepting writes on a zone-firewall controller, which
the window itself never exercises: the one declared forward is first
written in Wave D, once qbittorrent runs on the worker
([physical/gateway.md](../physical/gateway.md) §4.2). The probe runs
on scratch objects, on any day before the window
([physical/gateway-cutover.md](../physical/gateway-cutover.md) §3).

Security verifications (from the 2026-08-23 audit,
cluster/security-audit.md): a pod's request to `169.254.169.254` is
denied by the baseline policy; the UDM rejects an out-of-policy BGP
advertisement from the worker (bogus-prefix test, cluster-infra.md
§2); a prefix-scoped B2 key cannot list/delete a foreign prefix
(storage.md §4); the ExternalAuth filter fails closed with Authelia
down *and* the standing auth canary alerts (cluster-infra.md §2);
the Talos ingress firewall drops an undeclared port on a node
primary IP, **and** a declared raw TCP/UDP LoadBalancer service port
serves with no firewall entry (the BPF-precedence check — failure
flips the recorded public-port-census fallback, §2); the kubelet
answers the cluster and nothing else — once `k8s-base` is up,
`kubectl top nodes` lists every node, while 10250 at a node's public
address gets no answer from outside the VCN; and no `NodePort` is allocated — once `k8s-base` is up,
`kubectl get svc -A -o jsonpath='{..nodePort}'` prints nothing
(cluster-infra.md §2).
