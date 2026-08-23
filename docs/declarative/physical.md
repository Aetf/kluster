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
resources on the UDM, the Cloudflare zone + anchor records, and the B2
backup bucket.

Explicitly **not** owned: the state-backend E2.1.Micro (a bootstrap
dependency of Pulumi itself — hand-created, documented in
[framework/ci.md](../framework/ci.md) §1) and anything speaking the k8s
API (that's `k8s-base`/`apps`).

Stack outputs (the machine facts other stacks may reference,
pulumi.md §3.1): `kubeconfig`, `talosconfig`, per-node public/private
IPs, the NLB IP, the hath dedicated-VIP addresses (reserved public +
secondary private), and bucket names/endpoints.

## 1. OCI (pulumi-oci)

-   **VCN**: dual-stack (IPv4 + the assigned /56 GUA), one public
    subnet, internet gateway. Security lists/NSGs per
    architecture.md §5.1: 80/443 + raw service ports, 51820/udp,
    6443/50000, intra-VCN open.
-   **Image**: no official Talos OCI image — an `image_factory_schematic`
    (talos provider) pins the schematic (platform `oracle`, arm64, no
    extensions initially), and a custom-image import brings the
    factory-built image into OCI. The schematic ID is part of the
    declared state, so image contents are reproducible.
-   **Nodes**: 3× `VM.Standard.A1.Flex` (1 OCPU / 8 GB), spread across
    fault domains, boot volume ~50 GB. Machine config is delivered as
    base64 `user_data` in instance metadata (the Talos `oracle` platform
    reads the OCI metadata service) — day-0 needs no network apply.
-   **hath's node** additionally gets: a block volume (cache), a
    **secondary private IP** on its VNIC, and the **reserved public IP**
    assigned to it (the dedicated-VIP pattern, architecture.md §3.2).
-   **NLB**: one Network Load Balancer, listeners for 80/443, raw
    service ports, 6443, 50000; backend set = the three nodes;
    source/destination preservation per the §3.2 semantics
    (verification item).
-   **Buckets**: the JuiceFS chunk bucket on OCI Object Storage
    (storage.md §4/§6) + customer keys.
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
    cert SANs including the NLB IP; kubelet system-reserved so eviction
    actually works (the legacy CP-starvation lesson, architecture.md
    §6.5); the hath node's secondary private IP on its interface.
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
`upgrade-k8s`, and etcd snapshots are imperative operations run as mise
tasks/runbooks — not wrapped in fake-declarative command resources. The
official provider's v0.12 `talos_machine`/`talos_cluster` resources add
real drift detection and upgrade orchestration; **tracking item**: adopt
them for day-2 once v0.12 is stable *and* has reached the Pulumi bridge.

## 3. Homelab (pulumi-libvirt)

-   **Worker VM**: 12–16 vCPU / 20 GiB (nodes.md §4.2), bridged to the
    LAN, disk on NVMe (starts ~60 GB, grows as migration reclaims
    space). Talos via the `nocloud` image variant, machine config on a
    cloud-init seed ISO. UHD 770 VFIO passthrough + guest device plugin
    (early-verify item, nodes.md §4.1).
-   **HAOS**: the existing domain is `pulumi import`-ed and then
    declared — no rebuild, no cluster coupling (architecture.md §6.8).

## 4. UDM (gw-config dynamic provider + pulumiverse/unifi)

Per architecture.md §5.2: the gw-config provider (SSH, `/data`,
idempotent diff/apply, post-apply hooks) manages FRR/BGP (neighbor =
the worker VM's IP from the libvirt resource) and the nspawn estate
(units + digest-pinned rootfs from homelab-containers CI); the regular
unifi provider manages firewall rules (lan-pool subnet policy, the
qbittorrent v6 pinhole). No port forwards exist.

## 5. DNS anchors (pulumi-cloudflare) and B2

The zone resources plus the **anchor records only**: NLB A/AAAA under
the service hostname roots. Per-app records are declared beside their
apps (declarative/dns.md pattern). B2: the backup bucket, keys, and
lifecycle rules (storage.md §4-5).

## 6. Bootstrap order & verification checklist

Order within the first `pulumi up`: OCI network → instances (user_data
configs) ∥ libvirt VM → bootstrap (first CP) → health → outputs; NLB,
gw-config/FRR, and DNS anchors can settle in parallel once IPs exist.
Manual preconditions: OCI tenancy on PAYG, the state-backend micro
(ci.md §1), and the ZT route for the operator's first run.

Bootstrap-time verifications (carried from README #6 + this doc): LB
IPAM pool containing node primary IPs; NLB dual-stack listeners +
source-preservation semantics; etcd fsync latency on OCI block volumes;
A1 capacity at creation; Egress Gateway under the chosen routing mode +
reserved-IP↔secondary-private-IP NAT; talosctl reaching the homelab
node via cloud endpoints (apid proxy); VFIO iGPU passthrough into the
worker VM.
