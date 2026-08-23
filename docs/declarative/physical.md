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
    §6.5); the augmented node's secondary private IP on its interface.
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

-   **Worker VM**: 12–16 vCPU / 20 GiB (nodes.md §4.2), bridged to the
    LAN, disk on NVMe (starts ~60 GB, grows as migration reclaims
    space). Talos via the `nocloud` image variant, machine config on a
    cloud-init seed ISO.
-   **VM disk: a raw sparse file on the root btrfs, nodatacow, over
    virtio-blk** (decided 2026-08-23). The NVMe is a single btrfs
    partition (no LVM, no spare partitions), so a file it is — the
    shape makes that rational:
    -   **Dedicated subvolume with `chattr +C`** (nodatacow, set on the
        directory before image creation). VM images are the canonical
        CoW-on-CoW pathology — the guest's random small writes
        fragment a checksummed CoW file without bound. nodatacow
        trades btrfs checksums/compression for sane write behavior;
        integrity of the data that matters is owned by the *cluster's*
        backup regime, not the host fs. The subvolume boundary also
        keeps the image out of any host snapshot/send scope.
    -   **Raw, not qcow2**: with CoW disabled, qcow2's allocation layer
        buys nothing but indirection. Growth (60 → 100+ GB interleaved
        with reclamation, migration.md §0.4) is `truncate` on the file
        + `virsh blockresize`; Talos grows its EPHEMERAL partition
        into the new space on its own.
    -   **virtio-blk with `discard=unmap`, `cache=none`**: in-guest
        TRIM punches holes back out of the sparse file, so NVMe space
        actually returns to the host when the guest deletes data —
        load-bearing for the interleaved migration. `cache=none`
        avoids double-caching guest I/O through the host page cache.
    -   **What btrfs still contributes**: an offline `cp --reflink` of
        the image is a free instant copy before risky day-2 surgery
        (Talos upgrades are already A/B; this is belt-and-suspenders).
        Offline only — reflinking a running nodatacow image yields an
        inconsistent copy.
-   **VM network: a second host bridge, HAOS-pattern but not the HAOS
    bridge** (decided 2026-08-23). The *mechanism* is copied from the
    HAOS domain exactly — a systemd-networkd Linux bridge + virtio NIC
    tap — but the existing `kvmbr0` enslaves the **IoT VLAN**
    (192.168.90.0/24), which is where HAOS belongs with its devices
    and where the worker VM does not. The worker joins the host's
    untagged **server VLAN** (192.168.80.0/24 — the host, NAS serving,
    and the UDM BGP session all live there), which today has **no
    bridge**: `enp7s0` carries the host address directly. So the host
    network config (aconfmgr-managed systemd-networkd) gains a second
    bridge (say `kvmbr1`) enslaving `enp7s0`, with the host's
    address/DHCP moving onto the bridge — one brief connectivity blip,
    done in the same aconfmgr change-set that installs the libvirt
    resources. A real bridge, **not macvtap**: macvtap trades away
    host↔guest connectivity for zero host-network reconfiguration
    (frames from the host's own IP on the parent NIC can't hairpin
    back to a macvtap VM through an ordinary switch), and host↔VM is
    load-bearing here — NFS from the NAS role into the cluster,
    talosctl/management sessions from the host. Off-host traffic (the
    UDM's FRR session, LAN clients) would be fine either way; it is
    specifically the host side that macvtap severs. HAOS itself runs
    tap-on-bridge today (verified 2026-08-23: `vnet0` is a tap slaved
    to `kvmbr0`), so "copy the HAOS mechanism" and "real bridge" are
    the same statement.
    Addressing: **static IPv4 in the Talos machine config** (the UDM's
    FRR neighbor address must not depend on a DHCP lease) + SLAAC
    GUA/ULA for v6 (architecture.md §3.5's qbittorrent path expects
    the VM's SLAAC GUA).
-   **GPU is two-phase**: the VM bootstraps with
    no hostdev (host i915 keeps serving the legacy cluster), but its
    Talos schematic carries the i915 firmware extension from day 0
    (harmless without a GPU) so the later cutover touches no OS image.
    The cutover itself — drain → host binds vfio-pci → domain gains the
    PCI hostdev → reboot → device plugin sees the GPU — is a migration
    window sequenced with the immich/jellyfin move (migration.md §0).
    Implementation note: pulumi-libvirt's hostdev support is thin; the
    provider's XSLT escape hatch may be needed for the PCI device XML
    (the HAOS domain proves the libvirt side works).
-   **HAOS**: the existing domain is `pulumi import`-ed and then
    declared — no rebuild, no cluster coupling (architecture.md §6.8).

## 4. UDM (gw-config dynamic provider + pulumiverse/unifi)

Per architecture.md §5.2 (full push-direction absorption): the
gw-config provider (SSH, `/data`, idempotent diff/apply, post-apply
hooks) manages the device's entire desired state — FRR/BGP (neighbor =
the worker VM's IP from the libvirt resource), the nspawn estate
(units + digest-pinned rootfs from homelab-containers CI), on_boot.d,
caddy, AdGuard static configs, secrets. The gw-config repo retires;
periodic backup *pulls* move to a yadm timer on the homelab host. The
regular unifi provider manages firewall rules (lan-pool subnet policy,
the qbittorrent v6 pinhole) and any static LAN host entries
(dns.md §4). No port forwards exist.

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
(ci.md §1), and the ZT route for the operator's first run.

Bootstrap-time verifications (carried from README #6 + this doc): LB
IPAM pool containing node primary IPs; NLB dual-stack listeners +
source-preservation semantics; etcd fsync latency on OCI block volumes;
A1 capacity at creation; Egress Gateway under the chosen routing mode +
reserved-IP↔secondary-private-IP NAT; Cilium MTU over the KubeSpan
underlay; talosctl reaching the homelab node via cloud endpoints (apid
proxy); VFIO iGPU passthrough capability on a scratch VM.
