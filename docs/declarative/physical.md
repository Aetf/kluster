# Declarative Design: the `physical` Stack

How the physical layer is declared — providers, resource graph, and
bootstrap order for everything that must exist before the k8s API does.
The *why* of the architecture lives in
[cluster/architecture.md](../cluster/architecture.md); this document is
the *how* for the `physical` stack of
[framework/pulumi.md](../framework/pulumi.md) §3.

> **Status**: designed 2026-08-22; provider choices verified against
> current releases (pulumiverse-talos 0.8.1 wrapping the official
> siderolabs terraform-provider 0.11). Not implemented.

## 0. Scope and outputs

Owns: OCI (network, nodes, NLB, IPs, volumes, buckets, guardrails),
libvirt on the homelab host (worker VM + adopted HAOS domain), Talos
day-1 (secrets → configs → apply → bootstrap), the gw-config and unifi
resources on the UDM, and the B2 backup bucket. DNS lives in the `dns`
stack (declarative/dns.md), which consumes this stack's IP outputs.

Explicitly **not** owned: the state-backend E2.1.Micro (a bootstrap
dependency of Pulumi itself — hand-created, documented in
[framework/ci.md](../framework/ci.md) §1) and anything speaking the k8s
API (that's `k8s-base`/`apps`).

Stack outputs (the machine facts other stacks may reference,
pulumi.md §3.1): `kubeconfig`, `talosconfig`, per-node public/private
IPs, the NLB IP, the dedicated-VIP addresses (reserved public +
secondary private), and bucket names/endpoints.

## 1. OCI (pulumi-oci)

-   **VCN**: dual-stack (IPv4 + the assigned /56 GUA), one public
    subnet, internet gateway. Security rules are **derived, not
    enumerated**: the platform baseline (KubeSpan, Talos/kube
    management, intra-VCN) is declared here, while per-service ingress
    rules are emitted beside the services that need them (same
    co-location principle as DNS) — a hand-kept port list in a design
    doc would only ever be stale.
-   **Image**: no official Talos OCI image — an `image_factory_schematic`
    (talos provider) pins the schematic (platform `oracle`, arm64, no
    extensions initially), and a custom-image import brings the
    factory-built image into OCI. The schematic ID is part of the
    declared state, so image contents are reproducible.
-   **Nodes**: 3× `VM.Standard.A1.Flex` (1 OCPU / 8 GB), spread across
    fault domains, boot volume ~50 GB. Machine config is delivered as
    base64 `user_data` in instance metadata (the Talos `oracle` platform
    reads the OCI metadata service) — day-0 needs no network apply.
    **Legacy IMDS (v1) is disabled** on every instance; note OCI's v2
    auth header is a static string, so the control that actually keeps
    pods away from `user_data` (= the machine config, secrets included)
    is the baseline network policy (architecture.md §4.1,
    cluster-infra.md §2).
-   **One augmented node**: exactly one of the three additionally gets
    a block volume, a **secondary private IP** on its VNIC, and the
    **reserved public IP** assigned to it (the dedicated-VIP pattern,
    architecture.md §3.2). Nothing about the node is hath-specific —
    it is simply the node with extra storage and networking; hath (or
    any future dedicated-VIP workload) lands on it via scheduling
    constraints declared with the workload.
-   **NLB**: one Network Load Balancer with source-IP preservation
    (verification item) and a backend set of the three nodes, declared
    here; **listeners are not a fixed list** — the management listeners
    (6443/50000) live here, while service listeners are declared beside
    the services that need them, exactly like security rules and DNS
    records.
-   **Buckets**: the JuiceFS chunk bucket on OCI Object Storage
    (storage.md §4/§6) + customer keys.
-   **Protection**: data- and identity-bearing resources here — block
    volumes (hath's cache above all), buckets, the reserved public IP,
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

-   **Patches are Python.** Per-node machine config is composed from
    typed Python dicts (our framework's home turf), covering: KubeSpan
    on; KubePrism; dual-stack pod/service CIDRs **IPv4 first**
    (architecture.md §1.3); CP scheduling enabled
    (`allowSchedulingOnControlPlanes` — the combined CP+ingress role);
    cert SANs including the NLB IP; **etcd encryption at rest**
    (secretbox — the §6.5 residual-risk mitigation for cluster secrets
    in a $0-trust tenancy); kubelet system-reserved so eviction
    actually works (the legacy CP-starvation lesson, architecture.md
    §6.5); the **Talos ingress firewall** (`NetworkRuleConfig`,
    default-deny) as the node-local layer beneath the derived OCI
    rules (architecture.md §4.1) — enumeration rule (2026-08-24):
    **only ports that terminate in the host netns** — KubeSpan
    51820, apid 50000, kube-apiserver 6443 (a hostNetwork static
    pod, so host-side despite also being an NLB listener), kubelet
    intra-cluster, intra-VCN platform traffic; the homelab worker
    additionally BGP 179 from the UDM. **Service ports are
    deliberately absent**: LoadBalancer VIP traffic — NLB health
    checks on backend ports included — is intercepted by Cilium's
    BPF datapath at tc ingress *before* nftables, so declared
    frontends serve without firewall entries while undeclared ports
    fall through to the host stack and hit the default-deny; the
    two layers compose, per-service admission control *is* the KPR
    datapath, and machine config never carries an app port (the
    co-location principle survives). Verified both ways at
    bootstrap (§6); recorded fallback if BPF precedence fails on
    the chosen datapath mode: copy the small public-port census (a
    `conventions.py` constant — 80/443/22000×2/8443/hath, rarely
    changing) into machine config, accepting the cross-stack cost
    only in that world; kube-apiserver `anonymous-auth=false` pinned and audit
    logging on (a public 6443 warrants both, defaults notwithstanding);
    the augmented node's secondary private IP on its interface;
    the **local-path backing mount** (`/var/mnt/storage`, storage.md
    §2 — the StorageClass's provisioner is k8s-base's, but the disk
    path under it is machine config).
-   **Reboot-requiring config changes**: `apply_mode:
    staged_if_needing_reboot`, and CI applies node-serially so the
    quorum never reboots together.
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
    bridged to the LAN, disk on NVMe — both disk *and* RAM start
    smaller during migration and grow per wave (~60 GB / ~10 GiB at
    bootstrap; migration.md §0.4). The VM's **system design** — disk shape (raw sparse on a
    nodatacow subvolume, virtio-blk), the second host bridge, the
    two-phase GPU passthrough, and the host-prep aconfmgr change-set
    the program assumes — is
    **[physical/homelab-host.md](../physical/homelab-host.md)**; this
    section owns only how it is declared. Talos via the `nocloud`
    image variant, machine config on a cloud-init seed ISO — the seed
    carries the machine secrets: root-only permissions, and it lives
    outside every host snapshot/backup scope (the same subvolume
    discipline as the disk image).
-   **GPU hostdev**: not present at bootstrap (the two-phase plan,
    homelab-host.md §3); when the Wave C cutover adds it,
    pulumi-libvirt's hostdev support is thin — the provider's XSLT
    escape hatch may be needed for the PCI device XML (the HAOS
    domain proves the libvirt side works).
-   **HAOS**: the existing domain is `pulumi import`-ed and then
    declared — no rebuild, no cluster coupling (architecture.md §6.8).
    Mechanics on record: the libvirt provider imports domains **by
    UUID** (recent provider versions), and imported state is known to
    be incomplete for some attributes — expect an `ignore_changes`
    tuning pass until `preview` is clean, and verify the import early
    (a botched diff must never propose replacing this domain; its
    resource is `protect=True` like every data-bearing resource).
    **Adoption is mandatory** (decided 2026-08-23) — HAOS does not stay
    outside Pulumi. If import proves unworkable, the fallback is a
    **definition-layer takeover in a short scheduled window**: shut
    HAOS down, let Pulumi define the domain fresh pointing at the
    *existing* disk volumes and passthrough devices (the qcow2 and USB
    controller are the identity; the domain XML is just metadata),
    boot. Data is never migrated or recreated either way.

## 4. UDM (gw-config dynamic provider + bridged filipowm/unifi)

Per architecture.md §5.2 (full push-direction absorption): the
gw-config provider (SSH, `/data`, idempotent diff/apply, post-apply
hooks; the UDM's **SSH host key is pinned** in provider config — the
session crosses ZeroTier, and an accept-new first contact would hand
a MITM root on the gateway) manages the device's entire desired state — FRR/BGP (neighbor =
the worker VM's IP from the libvirt resource), the nspawn estate
(units + digest-pinned rootfs from homelab-containers CI — including
the **ZeroTier member container**, host-networking + `/dev/net/tun` +
`/data`-persisted identity, architecture.md §5.3), on_boot.d,
caddy, AdGuard static configs, secrets. ZT Central's network config
(managed routes via the UDM member, member authorizations) is managed
from the `physical` stack via the bridged `zerotier/zerotier`
provider (architecture.md §5.3). The gw-config repo retires;
periodic backup *pulls* move to a yadm timer on the homelab host.

The **unifi provider (filipowm/unifi via the Terraform bridge,
architecture.md §5.1)** manages the controller-side resources, **all
in this stack** — the co-location exception on record: gateway
resources follow the gateway's credential tier, so the `apps` CI
environment never holds a controller credential; app components keep
a pointer (workloads.md §4). The census: the IoT→lan-pool zone
policy with its address groups (architecture.md §3.4 — the v4 CIDR
group and the ULA group are separate objects, UniFi address groups
being single-family), the qbittorrent v6 pinhole and its v4
peer-port forward (**the only port forward**; no management inbound
exists), and any static LAN host entries (dns.md §4). Auth: a
dedicated local admin with an **API key** — never the SSH
credential — and failure retries are throttled: the UniFi global
login rate-limit is not per-IP and has locked out real users before
(the HA-integration incident).

## 5. B2 (bridged provider)

The backup bucket, keys, and lifecycle rules (storage.md §4-5). (DNS —
zones, estate records, anchors — moved to the `dns` stack,
declarative/dns.md.)

## 6. Bootstrap order & verification checklist

Order within the first `pulumi up`: OCI network → instances (user_data
configs) ∥ libvirt VM → bootstrap (first CP) → health → outputs; the
NLB and gw-config/FRR settle in parallel once IPs exist, and the `dns`
stack's anchors follow from the IP outputs.
Manual preconditions: OCI tenancy on PAYG, the state-backend micro
(ci.md §1), and the homelab host-prep change-set (§3). ZeroTier
Central config (managed routes via the UDM member, CI member
pre-auth, and the flow rules — physical/gateway.md §2) is
Pulumi-managed via the bridged zerotier provider (architecture.md
§5.3); only if that bridge proves unusable do those settings fall
back to hand-kept manual preconditions.

Bootstrap-time verifications (carried from README #6 + this doc; note
that several items exercise Cilium and therefore run only after
`k8s-base` is up — the gate's place in the sequence is
migration.md §1): LB IPAM pool containing node primary IPs; NLB dual-stack listeners +
source-preservation semantics; etcd fsync latency on OCI block volumes;
A1 capacity at creation; Egress Gateway under the chosen routing mode +
reserved-IP↔secondary-private-IP NAT; Cilium MTU over the KubeSpan
underlay; talosctl reaching the homelab node via cloud endpoints (apid
proxy); VFIO iGPU passthrough capability on a scratch VM; the bridged
filipowm/unifi provider round-tripping a scratch
`firewall_zone_policy` (create → clean diff → delete) against the
UDM's current Network release — the resource is experimental and
targets UniFi OS ≥9, and a failure here flips the rules to the
gw-config `UnifiFirewallPolicy` fallback (architecture.md §5.1) —
plus the legacy port-forward endpoint still accepting writes on a
zone-firewall controller.

Security verifications (from the 2026-08-23 audit,
cluster/security-audit.md): a pod's request to `169.254.169.254` is
denied by the baseline policy; the UDM rejects an out-of-policy BGP
advertisement from the worker (bogus-prefix test, cluster-infra.md
§2); a prefix-scoped B2 key cannot list/delete a foreign prefix
(storage.md §4); the ExternalAuth filter fails closed with Authelia
down *and* the standing auth canary alerts (cluster-infra.md §2);
the Talos ingress firewall drops an undeclared port on a node
primary IP, **and** a declared LoadBalancer service port serves
with no firewall entry (the BPF-precedence check — failure flips
the recorded public-port-census fallback, §2).
