# Physical Design: the Homelab Host & Worker VM

The system design of the homelab side's physical layer: how the worker
VM is shaped on the host (disk, network, GPU), and what the host
itself must provide. Sizing and the host inventory live in
[cluster/nodes.md](../cluster/nodes.md) §4; how these resources are
*declared* (pulumi-libvirt mechanics, HAOS import) lives in
[declarative/physical.md](../declarative/physical.md) §3.

> **Status**: designed 2026-08-23 (as declarative/physical.md §3),
> extracted to this topic 2026-08-24. Not implemented.

## 1. VM disk: raw sparse file on the root btrfs, nodatacow, virtio-blk

The NVMe is a single btrfs partition (no LVM, no spare partitions), so
a file it is — the shape makes that rational:

-   **Dedicated subvolume with `chattr +C`** (nodatacow, set on the
    directory before image creation). VM images are the canonical
    CoW-on-CoW pathology — the guest's random small writes fragment a
    checksummed CoW file without bound. nodatacow trades btrfs
    checksums/compression for sane write behavior; integrity of the
    data that matters is owned by the *cluster's* backup regime, not
    the host fs. The subvolume boundary also keeps the image out of
    any host snapshot/send scope.
-   **Raw, not qcow2**: with CoW disabled, qcow2's allocation layer
    buys nothing but indirection. Growth (60 → 100+ GB interleaved
    with reclamation, migration.md §0.4) is `truncate` on the file +
    `virsh blockresize`; Talos grows its EPHEMERAL partition into the
    new space on its own.
-   **virtio-blk with `discard=unmap`, `cache=none`**: in-guest TRIM
    punches holes back out of the sparse file, so NVMe space actually
    returns to the host when the guest deletes data — load-bearing
    for the interleaved migration. `cache=none` avoids double-caching
    guest I/O through the host page cache.
-   **What btrfs still contributes**: an offline `cp --reflink` of
    the image is a free instant copy before risky day-2 surgery
    (Talos upgrades are already A/B; this is belt-and-suspenders).
    Offline only — reflinking a running nodatacow image yields an
    inconsistent copy.

## 2. VM network: a dedicated cluster VLAN on a second host bridge

The *mechanism* is copied from the HAOS domain exactly — a
systemd-networkd Linux bridge + virtio NIC tap — but neither existing
network is the worker's. `kvmbr0` enslaves the **IoT VLAN**
(192.168.90.0/24), where HAOS belongs with its devices; the host's own
untagged **server LAN** (192.168.80.0/24 — the host address, the NAS
serving) is a shared population the cluster node has no business
joining. Cluster nodes get a network of their own: **VLAN id 7,
192.168.70.0/24, statically addressed, with no DHCP server on it**.

The isolation is the point. Every network in the UDM's LAN zone reaches
every other unconditionally (gateway.md §4.1), so a node on the server
LAN is a machine no policy can be written about; a node on its own VLAN
is a zone the gateway can police (gateway.md §4.2). Subnet numbering
follows the estate convention recorded in gateway.md §1.

So the host network config (aconfmgr-managed systemd-networkd) gains
three things, in the same change-set that installs the libvirt
resources (§4):

-   an **`enp7s0.7` tagged VLAN interface** on the physical NIC, which
    presumes the host's switch port carries VLAN 7 tagged — a
    port-profile fact on the UDM side, not a host one;
-   **`kvmbr1`, a bridge over `enp7s0.7`**, which the worker's tap
    joins. `enp7s0` itself is untouched and keeps carrying the host's
    untagged 192.168.80.x address, so nothing moves and there is no
    connectivity break to schedule;
-   a **host leg in the VLAN: 192.168.70.2 on `kvmbr1`**. This host is
    the NAS, and without a leg every NFS read by a VM one bridge away
    would hairpin out to the UDM and back for both directions of every
    packet. With it, host↔worker storage traffic stays on the box.

A real bridge, **not macvtap**: macvtap trades away host↔guest
connectivity for zero host-network reconfiguration (frames from the
host's own IP on the parent NIC can't hairpin back to a macvtap VM
through an ordinary switch), and host↔VM is load-bearing here — NFS
from the NAS role into the cluster, talosctl/management sessions from
the host. Off-host traffic (the UDM's FRR session, LAN clients) would
be fine either way; it is specifically the host side that macvtap
severs. HAOS itself runs tap-on-bridge today (verified 2026-08-23:
`vnet0` is a tap slaved to `kvmbr0`), so "copy the HAOS mechanism" and
"real bridge" are the same statement.

Addressing inside VLAN 7: **`.1` is the UDM's gateway address, `.2` the
host leg, and nodes run from `.10`** — the worker is **192.168.70.10**.
The v4 address is **static in the Talos machine config**, both because
the UDM's FRR names it as a BGP neighbor and because on day 1 apid has
no way to discover an address it was not told; with no DHCP on the VLAN
there is no lease, reservation or pool to reconcile it against. v6 is the SLAAC
GUA from the UDM's RA on this network (architecture.md §3.5's
qbittorrent path expects that GUA) plus the VLAN's ULA /64.

## 3. GPU: two-phase VFIO passthrough

The VM bootstraps with no hostdev (host i915 keeps serving the legacy
cluster), but its Talos schematic carries the i915 firmware extension
from day 0 (harmless without a GPU) so the later cutover touches no OS
image. The cutover itself — drain → host binds vfio-pci → domain gains
the PCI hostdev → reboot → device plugin sees the GPU — is a migration
window sequenced with the immich/jellyfin move (migration.md Wave C).
The capability is verified early on a scratch VM (bootstrap
verification, declarative/physical.md §6).

## 4. Host preparation: one aconfmgr change-set

A prerequisite the Pulumi program assumes rather than manages (the
host is not a Pulumi target; the same boundary as the NAS role). Its
contents, so nothing is discovered mid-bootstrap:

-   the `enp7s0.7` VLAN interface, the `kvmbr1` bridge over it, and the
    host's own 192.168.70.2 leg on that bridge (§2) — `enp7s0` and its
    untagged address are left alone;
-   the nodatacow subvolume (§1) + a libvirt storage pool pointing at
    it;
-   a **dedicated service user** and its SSH identity for the libvirt
    provider (`qemu+ssh://` over the CI ZeroTier join), declared like
    every other system account in the change-set — a `systemd-sysusers`
    entry plus the key — and a member of the `libvirt` group and no
    other. Note that `libvirt`-group access is root-*equivalent* in
    effect — domain XML can map any host device or disk — so this
    identity is guarded at the same tier as the UDM key, not as an
    unprivileged account. The private half is a `physical` config
    secret (credentials.md §3); creating and installing it is
    aconfmgr's, and pasting it into that config is the only step on the
    cluster's side;
-   the NAS NFS exports extended to the worker's VLAN-7 address,
    192.168.70.10 — scoped to that host, and served over the host leg
    rather than the untagged LAN.

The vfio-pci host binding is deliberately *not* here — it lands in the
Wave C cutover (§3, migration.md).
